"""CLI entry point for potluck.

potluck v1 does ONE thing: write code. The doer implements a spec, a
deterministic gate settles correctness, an independent model reviews the code
that was just written, a capped debate resolves disagreement, and a tiebreaker
(or the human) settles genuine deadlock.

    # Write code against a spec:
    potluck fix --spec "Fix the add() off-by-one" --acceptance "tests pass"
    potluck fix --spec-file spec.md

    # Set up which models fill each role on this machine:
    potluck resolve            # interactive
    potluck resolve --auto     # use Codex/Kimi if detected
    potluck resolve --claude-only
    potluck doctor             # show detection, write nothing

Backward-compatible: if no subcommand is given but --spec is present, runs fix.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import os
import signal
import sys
from pathlib import Path

from .config import HarnessConfig
from .orchestrator import (
    Orchestrator,
    RealDoerClient,
    ReviewerClient,
    TiebreakerClient,
    real_run_gate,
    real_restore_tree,
    bound_get_diff,
)
from . import resolve as resolve_mod


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_config(args) -> HarnessConfig:
    from . import paths
    profile_path = None
    if hasattr(args, "profile") and args.profile:
        named = paths.profiles_dir() / args.profile / "profile.toml"
        if named.exists():
            profile_path = named
        else:
            p = Path(args.profile)
            if p.exists():
                profile_path = p
    cfg = HarnessConfig.load(profile_path)
    if hasattr(args, "no_debate") and args.no_debate:
        cfg.debate_enabled = False
    if hasattr(args, "no_tiebreaker") and args.no_tiebreaker:
        cfg.debate.use_tiebreaker = False
    if hasattr(args, "max_rounds") and args.max_rounds is not None:
        cfg.debate.max_rounds = args.max_rounds
    return cfg


def write_log(result: dict, args):
    """Write the --log-file operator-only (0o600). It carries model output that
    can quote the operator's source (and, on a no-change run, the doer's full
    reply), so it must not inherit a umask-default world-readable mode. The
    mode is forced on an EXISTING file too: O_CREAT's mode applies only on
    creation, so fchmod covers the overwrite case."""
    if hasattr(args, "log_file") and args.log_file:
        fd = os.open(args.log_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(result, indent=2, default=str))
        print(f"\nFull log written to {args.log_file}")


def build_orchestrator(cfg: HarnessConfig,
                       allow_dirty: bool = False,
                       checkpointer=None,
                       gate_path: str | None = None,
                       import_restore=None,
                       progress=None) -> Orchestrator:
    """Wire a cfg into a production Orchestrator (real doer/reviewer/tiebreaker,
    real gate, config-bound get_diff, real tree restore). Pure: cfg ->
    Orchestrator, no argparse and no I/O, so a test can build the exact object
    cmd_fix runs and assert on the wiring (e.g. `orch.get_diff.diff_cfg is
    cfg.diff`).

    restore_tree is the real `git reset --hard` + `git clean -fd` when
    allow_dirty is False; when True (operator opted out of the clean-tree
    precondition), it is a no-op -- we cannot safely reset a tree whose
    baseline we don't own -- and we log a `tree_hygiene_disabled` event so the
    transcript shows hygiene was turned off.

    checkpointer (hardening Step-0 §2) enables resumable checkpoints; None (the
    default) keeps the exact non-resumable behaviour.

    gate_path (conductor §5) runs the gate from an external, absolute path instead
    of the in-tree default; None keeps `real_run_gate`'s `./.claude/verify.sh`. The
    Orchestrator invokes the gate callable with no arguments, so an external path is
    bound here (partial) rather than passed at the call site.

    import_restore (ruling 001 part 2) REPLACES the baseline restore for a run that
    imported an attested result: it resets to HEAD and then re-establishes the
    digest-verified imported state, so a retry never discards the previous round's
    work. Restoration stays wired; only its baseline changes. --allow-dirty still
    drops it, exactly as it drops the ordinary restore.

    progress (ruling 002 R2) is the run's ProgressWriter, told of each call's
    start and end; None (a bare `fix` with no run directory) reports nothing."""
    doer = RealDoerClient()
    reviewer = ReviewerClient(cfg)
    tiebreaker = TiebreakerClient(cfg) if cfg.debate.use_tiebreaker else None
    restore_tree = None if allow_dirty else (import_restore or real_restore_tree)
    run_gate = (real_run_gate if gate_path is None
                else functools.partial(real_run_gate, gate_path))
    orch = Orchestrator(cfg, doer, reviewer, tiebreaker, run_gate,
                        bound_get_diff(cfg.diff), restore_tree=restore_tree,
                        checkpointer=checkpointer)
    orch.runner.progress = progress
    if allow_dirty:
        orch._log("tree_hygiene_disabled", allow_dirty=True)
    return orch


def _effective_config_dict(cfg: HarnessConfig) -> dict:
    """The resolved timeouts + caps actually in force (hardening Step-0 §2.1):
    the identity binds this so a config change that would alter timeouts/caps
    refuses a resume. Deliberately EXCLUDES machine-local model backends (those
    differ per machine and are not a run commitment)."""
    t = cfg.timeouts
    return {
        "model_call_seconds": t.model_call_seconds,
        "gate_seconds": t.gate_seconds,
        "feature_seconds": t.feature_seconds,
        "doer_step_seconds": dict(sorted(t.doer_step_seconds.items())),
        "doer_timeout_override": t.doer_timeout_override,
        "max_resume_retries": t.max_resume_retries,
        "backoff_delay_seconds": t.backoff_delay_seconds,
        "fixed_call_budget": cfg.call_budget.fixed_call_budget,
        "per_round_call_budget": cfg.call_budget.per_round_call_budget,
        "max_rounds": cfg.debate.max_rounds,
        # Ruling 002 R3: the retry policy is a run commitment, so a resume across
        # a change of it is refused by identity, exactly as a changed timeout is.
        "retries_before_counting": cfg.escalation.retries_before_counting,
    }


def _git_head() -> str:
    import subprocess
    proc = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not read git HEAD (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout.strip()


def _git_toplevel() -> Path:
    """The git working-tree ROOT (`git rev-parse --show-toplevel`), or cwd outside
    a git repo. The whole checkpoint machinery -- containment refusal AND the
    snapshot/restore/submodule-refusal -- must be rooted here, NOT at the
    invocation cwd, or a run from a subdirectory scopes git ops to that subtree
    and misses sibling nested repos / D-F transitions (codex round-6/7)."""
    import subprocess
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return Path.cwd().resolve()
    return Path(proc.stdout.strip()).resolve()


def _refuse_run_dir_inside_tree(run_dir: str) -> None:
    """Refuse a run-dir inside the working tree (fail-closed). §2.3 restore does
    `git clean -fd`, which would DELETE a non-ignored run-dir anywhere under the
    repo, and the tree snapshot would CAPTURE it -- both corrupt the checkpoint. The sidecar must live outside the tree it snapshots.

    Containment is checked against the git WORKING-TREE ROOT (`git rev-parse
    --show-toplevel`), NOT cwd (codex round-6 MAJOR): invoking from a subdirectory
    with `--run-dir ../runs/x` can resolve OUTSIDE cwd yet INSIDE the repo, which a
    cwd-based check would wrongly accept and clean -fd would still destroy."""
    rd = Path(run_dir).resolve()
    root = _git_toplevel()
    if rd == root or root in rd.parents:
        print(f"Error: --run-dir/--resume path ({rd}) is INSIDE the git working "
              f"tree ({root}). The checkpoint sidecar must live OUTSIDE the tree "
              f"it snapshots -- restore's `git clean -fd` would delete it and "
              f"the snapshot would capture it. Use a path outside the repo, e.g. "
              f"../potluck-runs/<id>.", file=sys.stderr)
        sys.exit(1)


def _run_identity(cfg: HarnessConfig, spec: str, acceptance: str):
    """(config digest, base commit, run manifest id) for this run's inputs."""
    from . import checkpoint as ck
    cfg_digest = ck.config_digest(_effective_config_dict(cfg))
    base = _git_head()
    return cfg_digest, base, ck.compute_manifest_id(spec, acceptance, cfg_digest, base)


def _build_checkpointer(run_dir: str, cfg: HarnessConfig, spec: str,
                        acceptance: str, gate_binding_digest: str):
    """Construct a git-backed Checkpointer for `run_dir` bound to this run's
    identity inputs (hardening Step-0 §2), including the gate binding (conductor §5)."""
    from . import checkpoint as ck
    _refuse_run_dir_inside_tree(run_dir)
    cfg_digest, base, manifest_id = _run_identity(cfg, spec, acceptance)
    # Root the snapshot/restore engine at the git WORKING-TREE ROOT, not cwd
    # (codex round-7): from a subdirectory, a cwd-scoped store would miss sibling
    # nested repos (the mandatory submodule refusal) and sibling D-F transitions
    # (losslessness), and clean/ls-files would only cover the subtree.
    store = ck.GitCheckpointStore(Path(run_dir), repo=_git_toplevel())
    return ck.Checkpointer(store, spec, acceptance, cfg_digest, manifest_id,
                           gate_binding_digest=gate_binding_digest), \
        cfg_digest, base, manifest_id


class _RunWorktree:
    """The run's OWN git worktree, detached at the run's base commit
    (FU-ref-ownership ruling: potluck must never move a ref the run does not
    exclusively control). The doer, the gate, the diff, the restore and the
    checkpoint engine all run with cwd inside it, so every reset they do moves
    only this worktree's detached HEAD -- a ref the run owns -- and the
    operator's branch ref, HEAD, index and working tree are never written during
    the run. A foreign session committing to the operator's branch mid-run is
    therefore untouched by anything the run does, abandonment included.

    PLACEMENT (the sibling-path trap): the worktree is created as a hidden
    SIBLING of the operator's checkout (same parent directory). The fleet
    resolves sibling repos by RELATIVE path -- menu's pyproject declares
    `harness-core = { path = "../harness_core" }` and the `./test` scripts
    default CORE to `$REPO/../harness_core` -- so a worktree anywhere else would
    resolve `../harness_core` to nothing and the gate would fail as a toolchain
    error (exit 2, no signal) that looks like the run's fault. As a sibling,
    every `../<repo>` resolves exactly as it does from the operator's checkout.
    If the parent directory is not writable we refuse, naming this cause, rather
    than fall back to a location where the gate cannot see its siblings.

    OUTPUT ON SUCCESS (decided): the run's change is APPLIED to the operator's
    working tree as uncommitted change (`git apply`, working tree only). That
    keeps potluck's existing contract -- the work is left as uncommitted change
    in the operator's tree -- and moves no ref. Merely reporting the worktree
    path was rejected: it breaks that contract for every caller (conductor,
    /potluck, a human), and it would require the worktree to outlive the run,
    which is exactly the orphan accumulation this must not cause. `git apply` is
    all-or-nothing, so if the operator's tree has diverged in an overlapping way
    nothing is written; the patch is saved outside the tree and its path
    reported instead. A worktree whose HEAD left the base (the doer COMMITTED)
    is not applied: the committed part never reached the review diff
    (`git diff HEAD`), so applying it would deliver unreviewed work.

    OUTPUT ON FAILURE (decided): nothing. Any non-PASSED outcome writes nothing
    to the operator's tree -- not even a restore, because nothing was ever
    written there -- and the worktree is removed. A checkpointed run's work
    survives in its run-dir checkpoint and is reconstructed by `--resume` into a
    fresh worktree.

    The worktree is removed when the run ends, on every path (cmd_fix's
    `finally`), together with its `.git/worktrees` administrative entry."""

    def __init__(self):
        self.root: Path | None = None   # the operator's checkout
        self.path: Path | None = None   # the run's worktree
        self.base: str | None = None    # the commit it is detached at
        self._cwd0: str | None = None

    def enter(self) -> None:
        """Create the worktree at the operator's HEAD and chdir into it (at the
        same relative subdirectory the run was invoked from)."""
        import subprocess
        import tempfile
        root = _git_toplevel()
        try:
            base = _git_head()
        except RuntimeError as e:
            print(f"Error: {e}. potluck fix runs in its own git worktree, so it "
                  f"needs a git repository with at least one commit.",
                  file=sys.stderr)
            sys.exit(1)
        try:
            # NO SWEEP OF EARLIER ORPHANS, and the reason is recorded because
            # one was written, reviewed and REMOVED (codex BLOCKING, r5). A
            # sweep must tell an orphan from a live concurrent run, and on the
            # filesystem it cannot: a concurrent run's directory exists from
            # `mkdtemp` but is not in `git worktree list` until `worktree add`
            # registers it, so a sweep would delete a LIVE run inside that
            # window -- far worse than the orphan it clears. Meanwhile a
            # SIGKILL-orphaned worktree that WAS registered stays listed, so the
            # sweep preserves exactly what it was meant to remove. Wrong in both
            # directions, from one TOCTOU race.
            #
            # DECLARED GAP, blast radius: a hard kill (SIGKILL, power loss)
            # bypasses every `finally` and leaves a `.<repo>.potluck-run-*`
            # directory beside the checkout, plus possibly a `.git/worktrees`
            # entry. Nothing removes it automatically; `git worktree prune` and
            # deleting the directory are the manual remedy. It costs disk and
            # tidiness, never correctness: the operator's refs, tree and commits
            # are untouched, which is what this whole change is for.

            # Claim the directory the INSTANT it exists (codex BLOCKING, r3).
            # mkdtemp CREATES it, so recording self.path after the call left the
            # same window one step earlier than r1 closed: a crash in between
            # orphans a directory beside the operator's checkout that remove()
            # cannot see. self.root is set first so remove() has a cwd to prune
            # from, and mkdtemp's return is assigned straight onto self.path --
            # there is no longer a local the claim can lag behind.
            #
            # IRREDUCIBLE REMAINDER, stated rather than implied: a kill
            # between mkdtemp's syscall returning and this assignment still
            # orphans the directory. No in-process ordering closes that --
            # it needs a DURABLE record of the intended path written before
            # creation, so a later run can sweep it. Declared, not built:
            # the window is now one assignment wide, against a whole
            # `git worktree add` before r1.
            #
            # NO TEST PINS THIS ORDERING, reported rather than papered over:
            # r1's claim already sits between mkdtemp and the add, so the gap
            # this narrows contains NO I/O and nothing in-process can observe
            # it. A test was written and DELETED on finding it passed with the
            # change reverted. The change is kept because it is free and
            # strictly safer, not because it was demonstrated.
            self.root = root
            self.path = path = Path(tempfile.mkdtemp(prefix=f".{root.name}.potluck-run-",
                                         dir=root.parent))
        except OSError as e:
            print(f"Error: cannot create the run's worktree beside {root} ({e}). "
                  f"It must be a SIBLING of the checkout: sibling repos are "
                  f"resolved by relative path (e.g. ../harness_core), and a "
                  f"worktree placed anywhere else would make the gate fail as a "
                  f"toolchain error (exit 2) that is not the run's fault.",
                  file=sys.stderr)
            sys.exit(1)
        # RECORD BEFORE CREATING (codex BLOCKING, r1). `worktree add` makes a
        # directory AND a `.git/worktrees` entry; recording `self.path` only
        # after it returned left a window in which a crash or SIGINT orphaned
        # both, because `cleanup()` short-circuits on `self.path is None` and
        # nothing else ever looks for them. Recording first means the window
        # contains no state cleanup cannot see: `cleanup()` tolerates a path
        # that was never created (remove fails -> rmtree ignore_errors ->
        # prune), so claiming the path early is strictly safer than claiming it
        # late.
        self.base = base
        proc = subprocess.run(
            ["git", "worktree", "add", "--detach", "--quiet", str(path), base],
            cwd=str(root), capture_output=True, text=True)
        if proc.returncode != 0:
            # Do NOT clear self.path here (codex BLOCKING, r2). A nonzero
            # `worktree add` is not a guarantee that nothing was created: it can
            # leave a partial directory and/or a `.git/worktrees` entry behind.
            # Clearing the path would make remove() short-circuit on exactly the
            # wreckage it exists to clear -- the same orphan the r1 fold closed,
            # reintroduced on the failure path. remove() already tolerates a path
            # that was never created, so leaving it claimed is strictly safer.
            print(f"Error: could not create the run's worktree (git worktree add "
                  f"exited {proc.returncode}): {proc.stderr.strip()[:300]}",
                  file=sys.stderr)
            sys.exit(1)
        self._cwd0 = os.getcwd()
        os.chdir(path / Path(self._cwd0).resolve().relative_to(root))

    def leave(self) -> None:
        if self._cwd0 is not None:
            os.chdir(self._cwd0)
            self._cwd0 = None

    def _git(self, *args, cwd=None, env=None, data=None):
        import subprocess
        return subprocess.run(["git", *args], cwd=cwd or str(self.path), input=data,
                              env={**os.environ, **env} if env else None,
                              capture_output=True)

    def change(self) -> tuple[str | None, str | None, bytes]:
        """(error, output tree OID, binary patch base -> that tree). The output tree
        is the worktree's final state, untracked non-ignored files included. A
        worktree whose HEAD left the base is refused here, so it can neither be
        delivered nor exported."""
        import tempfile
        git = self._git
        head = git("rev-parse", "HEAD").stdout.decode().strip()
        if head != self.base:
            return ("the doer COMMITTED in the run's worktree (HEAD moved off the "
                    "base), so part of its work never reached the review diff; "
                    "nothing was applied to your tree"), None, b""
        # The station_run/_work_tree_oid recipe: a temp index seeded from HEAD, so
        # the real index is untouched and untracked-ignored paths stay out.
        with tempfile.TemporaryDirectory() as tmpdir:
            wenv = {"GIT_INDEX_FILE": os.path.join(tmpdir, "index")}
            steps = [git("read-tree", "HEAD", env=wenv), git("add", "-A", env=wenv),
                     git("write-tree", env=wenv)]
        tree = steps[-1].stdout.decode().strip()
        diff = git("diff", "--binary", self.base, tree)
        failed = next((p for p in [*steps, diff] if p.returncode != 0), None)
        if failed is not None:
            return (f"could not compute the run's change: "
                    f"{failed.stderr.decode()[:300]}"), None, b""
        return None, tree, diff.stdout

    def apply_import(self, patch: bytes, want_digest: str, want_base: str) -> str | None:
        """Apply an imported cumulative patch to this worktree (fresh, detached at the
        operator's HEAD) and require the resulting full tree to digest to the imported
        result's attested `output_tree_digest`. Returns None, or an error naming why.

        The worktree's base must BE the imported manifest's `base_commit` (ruling 001:
        every round starts at the original base and exports its cumulative patch
        against that same base). Checked FIRST: a moved HEAD -- even an empty commit,
        which leaves every tree digest unchanged -- would otherwise import cleanly and
        chain a different base into the next export.
        `git apply` is all-or-nothing, and it runs here, never in the operator's tree."""
        from . import station_run as sr
        if self.base != want_base:
            return (f"the run's starting HEAD {self.base} is not the imported result's "
                    f"base_commit {want_base}; every round must start at the original "
                    f"base")
        if patch:
            applied = self._git("apply", "--binary", "-", data=patch)
            if applied.returncode != 0:
                return (f"the imported patch does not apply to the fresh worktree at "
                        f"{self.base}: {applied.stderr.decode().strip()[:300]}")
        got = sr._output_tree_digest(self.path)
        if got != want_digest:
            return (f"the imported patch produces tree digest {got}, not the imported "
                    f"result's attested output_tree_digest {want_digest}")
        return None

    def import_restore(self, patch: bytes, want_digest: str, want_base: str):
        """The restore for an importing run: reset this worktree to HEAD, then
        re-establish the digest-verified imported state. Raises on any failure, which
        the orchestrator attributes to the restore."""
        async def _restore() -> None:
            for args in (("reset", "--hard", "--quiet"), ("clean", "-fd", "--quiet")):
                proc = self._git(*args)
                if proc.returncode != 0:
                    raise RuntimeError(f"git {args[0]} failed: "
                                       f"{proc.stderr.decode()[:300]}")
            err = self.apply_import(patch, want_digest, want_base)
            if err is not None:
                raise RuntimeError(f"could not re-establish the imported state: {err}")
        return _restore

    def deliver(self) -> str | None:
        """Apply the run's change (base -> the worktree's final tree, untracked
        non-ignored files included) to the operator's working tree. Returns None
        on success, else an error message; writes nothing to the operator's tree
        on any failure."""
        import tempfile
        err, _tree, patch = self.change()
        if err is not None:
            return err
        if not patch:
            return None
        applied = self._git("apply", cwd=str(self.root), data=patch)
        if applied.returncode == 0:
            return None
        fd, saved = tempfile.mkstemp(prefix="potluck-unapplied-", suffix=".patch")
        with os.fdopen(fd, "wb") as f:
            f.write(patch)
        return (f"the run's change does not apply to your working tree, which "
                f"changed during the run ({applied.stderr.decode().strip()[:300]}); "
                f"nothing was written to it. The change is saved at {saved}")

    def remove(self) -> None:
        """Remove the worktree and its administrative entry. Never touches the
        operator's tree or any ref."""
        import shutil
        import subprocess
        self.leave()
        if self.path is None:
            return
        # The report below is A5's required output, so it must not be skipped by
        # an exception raised while cleaning up (codex BLOCKING, r7). The removal
        # attempts are wrapped; whatever happens, the survival check runs and the
        # operator is told.
        # FINALLY, not `except Exception` (codex BLOCKING, r8). Ctrl-C raises
        # KeyboardInterrupt, which derives from BaseException and so slipped
        # past an `except Exception` — bypassing the one report A5 requires, in
        # exactly the interrupted-run case an operator most needs it. A bare
        # `except BaseException` would swallow the interrupt; `finally` reports
        # and lets it propagate.
        try:
            self._attempt_removal()
        except Exception as e:                      # noqa: BLE001 -- report, never swallow
            print(f"Warning: the run's worktree cleanup raised "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        finally:
            try:
                self._report_survivors()
            except BaseException as e:   # noqa: BLE001 -- last resort, see below
                # The report itself can raise (codex r9): a stat on an odd path,
                # a subprocess that cannot spawn. A5 requires that an incomplete
                # cleanup is always SAID, so the last resort says it with no
                # inspection at all rather than saying nothing. BaseException is
                # deliberate here and safe: this is the innermost handler, it
                # re-raises nothing only because the outer `finally` has already
                # let the original exception through.
                print(f"Warning: the run's worktree may still be at {self.path}; "
                      f"the cleanup check itself failed ({type(e).__name__}: {e}). "
                      f"Run `git worktree prune` in {self.root} and delete the "
                      f"directory if it is there.", file=sys.stderr)
            finally:
                self.path = None

    def _attempt_removal(self) -> None:
        import shutil
        import subprocess
        proc = subprocess.run(
            ["git", "worktree", "remove", "--force", "--force", str(self.path)],
            cwd=str(self.root), capture_output=True)
        if proc.returncode != 0:
            shutil.rmtree(self.path, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(self.root),
                           capture_output=True)

    def _report_survivors(self) -> None:
        import subprocess
        # REPORT a cleanup that did not work (codex BLOCKING, r4). Both fallbacks
        # above swallow their own failure -- rmtree(ignore_errors=True) by
        # definition -- so without this check a run could leave its worktree
        # behind and say nothing, which is the silence A5 exists to prevent.
        # (2) CHECK THE ADMIN ENTRY TOO (codex BLOCKING, r6). `git worktree
        # prune` above has its failure discarded, and a stale `.git/worktrees`
        # entry is invisible to a directory check -- so a cleanup could leave
        # one and report success. Both halves are checked, and the message says
        # which survived.
        left_dir = Path(self.path).exists()
        # UNVERIFIABLE IS NOT ABSENT (codex BLOCKING, r7). If `worktree list`
        # itself fails we cannot see whether an admin entry survived, and
        # treating that as "absent" would report a clean cleanup on no evidence
        # -- fail-open, in the one message whose job is to tell the operator
        # something was left behind.
        listed = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                cwd=str(self.root), capture_output=True, text=True)
        if listed.returncode != 0:
            left_admin, admin_word = True, "a .git/worktrees entry (UNVERIFIABLE -- " \
                                           "`git worktree list` failed)"
        else:
            left_admin = str(self.path) in (listed.stdout or "")
            admin_word = "a .git/worktrees entry"
        if left_dir or left_admin:
            what = " and ".join(
                [s for s in ("the directory" if left_dir else "",
                             admin_word if left_admin else "") if s])
            # NOTE: no sweep exists (removed in r5), so this is NOT cleaned up
            # later. Saying otherwise would be a promise the code does not keep.
            print(f"Warning: the run's worktree could not be fully removed -- "
                  f"{what} survives at {self.path}. Nothing removes it "
                  f"automatically: run `git worktree prune` in {self.root} and "
                  f"delete the directory.", file=sys.stderr)
        self.path = None


def ensure_clean_tree(allow_dirty: bool = False) -> str | None:
    """Clean-tree precondition for `potluck fix`. Returns an actionable error
    message when the working tree is dirty (`git status --porcelain` non-empty)
    and --allow-dirty was NOT passed, else None. Extracted from cmd_fix so the
    precondition is testable without driving the whole CLI.

    Re-examined for the run-owned worktree: the run no longer writes the
    operator's tree, so this is no longer what lets a restore reset safely (the
    worktree is created clean at HEAD). It is KEPT for two reasons that remain:
    (1) the worktree is checked out at HEAD, so uncommitted changes would be
    invisible to the doer and the gate -- the run would silently judge a tree
    other than the one the operator sees; and (2) on success the change is
    applied to this tree, and a clean baseline makes the result exactly the
    run's change, never interleaved with the operator's own uncommitted work.

    Safe-default rule: if we cannot PROVE the tree is clean, we refuse. ANY
    failure to run the check refuses -- empty porcelain output is only
    meaningful when the command actually SUCCEEDED. Outside a git repo, with
    git missing from PATH, or when git exists but isn't executable, stdout is
    empty too and `porcelain.strip()` is falsy, which would silently read as
    "clean" (worse than a false failure: a failed check that passes). So we
    check the OUTCOME, not just stdout -- the same lesson the gate learned
    (green comes from exit status, not from empty output). Only an exit-0 status
    with empty porcelain output may return None."""
    if allow_dirty:
        return None
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
        )
    except OSError as e:
        # Outcome, not output: a launch failure is NOT a clean tree. OSError is
        # the parent of both FileNotFoundError (git absent) and PermissionError
        # (git present but not executable); catching the family keeps the
        # safe-default refusal the docstring promises instead of letting a
        # PermissionError crash the CLI.
        return (f"could not run git: {e}. Install git (or fix its "
                f"permissions), or pass --allow-dirty to skip the check.")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return (f"could not verify the working tree (git status exited "
                f"{proc.returncode}): {stderr[:200]}. Run potluck inside a git "
                f"repository, or pass --allow-dirty to skip the check.")
    if proc.stdout.strip():
        return ("working tree is not clean. Commit, stash, or pass "
                "--allow-dirty to run against the current tree.")
    return None


# ---------------------------------------------------------------------------
# fix — the code-writing loop
# ---------------------------------------------------------------------------

def add_fix_parser(subparsers):
    p = subparsers.add_parser("fix", help="Code-writing loop: doer → gate → reviewer → debate")
    p.add_argument("--spec", type=str, help="What to build / fix (inline)")
    p.add_argument("--spec-file", type=str, help="Read spec from a file")
    p.add_argument("--acceptance", type=str, default="All tests pass.",
                   help="Acceptance criteria")
    p.add_argument("--acceptance-file", type=str, help="Read acceptance from a file")
    p.add_argument("--gate-file", type=str, default=None,
                   help="Run the verification gate from an EXTERNAL path OUTSIDE the "
                        "working tree (conductor §5 seam), instead of the in-tree "
                        ".claude/verify.sh. The tree stays clean and the gate is never "
                        "an unattributed commit; its digest is bound into the run so a "
                        "--resume with a swapped gate fails closed.")
    p.add_argument("--profile", type=str, default=None)
    p.add_argument("--no-debate", action="store_true")
    p.add_argument("--no-tiebreaker", action="store_true")
    p.add_argument("--max-rounds", type=int, default=None)
    p.add_argument("--gate-timeout", type=int, default=None)
    p.add_argument("--doer-timeout", type=int, default=None,
                   help="Uniform per-doer-call timeout (seconds) overriding the "
                        "step-adaptive defaults for EVERY doer step. Re-validated "
                        "against the outer feature bound (hardening Step-0 §1).")
    p.add_argument("--allow-dirty", action="store_true",
                   help="Run against a dirty working tree; skips the clean-tree "
                        "precondition AND all tree-restore behavior. The run's "
                        "worktree is checked out at HEAD, so uncommitted changes "
                        "are NOT visible to the run; on success its change is "
                        "applied on top of them (refused if they overlap)")
    p.add_argument("--run-dir", type=str, default=None,
                   help="Persist a resumable checkpoint under this directory "
                        "(hardening Step-0 §2). A timed-out run can then be "
                        "continued with `potluck fix --resume <run-dir>`.")
    p.add_argument("--resume", type=str, default=None, metavar="RUN_DIR",
                   help="Resume a previously checkpointed run from its run-dir: "
                        "the spec/acceptance/config are read back from there, the "
                        "identity is re-verified fail-closed, the git tree is "
                        "restored losslessly, and the failed call re-runs with "
                        "backoff (hardening Step-0 §2). Ignores --spec/--run-dir.")
    p.add_argument("--export-dir", type=str, default=None,
                   help="Do NOT apply the run's change to this working tree. On "
                        "PASSED, write a portable result into this directory (which "
                        "must be OUTSIDE the git tree): a binary cumulative patch from "
                        "the base, station-run.json, the exact spec/acceptance/gate "
                        "inputs, and export-manifest.json, published last. A passed "
                        "run whose export cannot be completed fails. Requires --run-dir "
                        "and --export-task.")
    p.add_argument("--export-task", type=str, default=None,
                   help="The task identifier recorded in the export manifest. Required "
                        "with --export-dir.")
    p.add_argument("--export-round", type=int, default=0,
                   help="The round number recorded in the export manifest.")
    p.add_argument("--import-dir", type=str, default=None,
                   help="Start from a previous round's exported result instead of "
                        "committed HEAD: the export is verified fail-closed, its "
                        "cumulative patch applied to the run's fresh worktree at HEAD, "
                        "and the resulting tree must equal its attested "
                        "output_tree_digest before the doer runs. The directory is "
                        "only read. Requires --export-dir: the result is delivered "
                        "later by `potluck deliver`. Not supported with --resume.")
    p.add_argument("--json-output", action="store_true")
    p.add_argument("--log-file", type=str, default=None)
    p.set_defaults(func=cmd_fix)


def _apply_timeout_overrides(args, cfg) -> None:
    """Apply the CLI timeout overrides POST-load and re-validate (§1.2); exits on
    an invalid override. Shared by the fresh and resume paths so a resumed run's
    effective config (hence its config_digest) is computed identically."""
    # `is not None`, not truthiness: a 0 override is a valid (immediate-fire)
    # value, not "absent" -- truthiness would silently retain the profile value.
    if args.gate_timeout is not None:
        cfg.timeouts.gate_seconds = args.gate_timeout
    if args.doer_timeout is not None:
        cfg.timeouts.doer_timeout_override = args.doer_timeout
    # ALWAYS re-validate (codex round-9): --max-rounds (applied in load_config,
    # AFTER load()'s validate) raises N_max -> outer_min, and --no-debate/
    # --no-tiebreaker also change the effective config. Only gate/doer-timeout
    # were re-validated before, so `--max-rounds 100` could start with an
    # outer_min the feature bound no longer dominates (§1.2/§1.3 require
    # post-override validation).
    try:
        cfg.validate()
    except ValueError as e:
        print(f"Error: invalid config override: {e}", file=sys.stderr)
        sys.exit(1)


def _require_gate(gate_file: str | None = None) -> None:
    """Require a runnable gate. With --gate-file (conductor §5) the gate is the
    external path and the in-tree .claude/verify.sh is NOT required (a product tree
    may not carry one); otherwise the in-tree gate must exist."""
    if gate_file is not None:
        if not Path(gate_file).is_file():
            print(f"Error: --gate-file not found: {gate_file}", file=sys.stderr)
            sys.exit(1)
        return
    if not Path(".claude/verify.sh").exists():
        print("Error: .claude/verify.sh not found.\n"
              "Copy verify.sh.example into your project as .claude/verify.sh.",
              file=sys.stderr)
        sys.exit(1)


# The run-owned, IMMUTABLE frozen copy of an external gate, living in the run-dir
# (outside the working tree) — the bytes actually executed and digested (conductor §5).
_FROZEN_GATE_NAME = "frozen-gate.sh"


def _digest_gate_file(path: str) -> str | None:
    """sha256 (station_run.gate_digest_of_bytes) of the gate at `path`, or None if
    unreadable. The §4f station artifact's gate_digest is this over the gate ACTUALLY
    run (the frozen external copy, or the in-tree .claude/verify.sh)."""
    from . import station_run as sr
    try:
        return sr.gate_digest_of_bytes(Path(path).read_bytes())
    except OSError:
        return None


def _refuse_gate_inside_tree(gate_file: str) -> None:
    """Refuse an external gate whose path is INSIDE the working tree (conductor §5,
    codex MAJOR). An in-tree gate is a mutable file the doer can rewrite before the
    gate runs — the exact in-tree mutable-gate hazard §5 forbids — so the external
    gate must live outside the tree. Containment is checked against the git
    WORKING-TREE ROOT, matching `_refuse_run_dir_inside_tree`."""
    g = Path(gate_file).resolve()
    root = _git_toplevel()
    if g == root or root in g.parents:
        print(f"Error: --gate-file ({g}) is INSIDE the git working tree ({root}). "
              f"The external gate must live OUTSIDE the tree so the doer cannot mutate "
              f"it before it runs (conductor §5).", file=sys.stderr)
        sys.exit(1)


def _freeze_gate(gate_file: str, run_dir: str):
    """Copy the external gate's bytes to an IMMUTABLE run-owned path (outside the
    tree) and return (frozen_path, digest) (conductor §5, codex MAJOR/BLOCKING). The
    gate ACTUALLY RUN is this frozen copy, so its digest is provably the digest of the
    executed bytes even if the source changes after capture (handoff regeneration, a
    doer edit). The frozen copy persists in the run-dir, so a `--resume` re-runs the
    SAME gate without needing --gate-file re-passed and can verify it fail-closed."""
    from . import station_run as sr
    data = Path(gate_file).read_bytes()
    dest = Path(run_dir)
    dest.mkdir(parents=True, exist_ok=True)
    frozen = dest / _FROZEN_GATE_NAME
    tmp = dest / (_FROZEN_GATE_NAME + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, frozen)
    return str(frozen), sr.gate_digest_of_bytes(data)


def _resolve_resume_gate(resume_dir: str):
    """Resolve the gate for a resume from a FILE FACT — the frozen copy's presence —
    NOT an editable run_inputs field (conductor §5, codex §5 r2 BLOCKING). Returns
    (effective_gate_path, gate_binding_digest); `--gate-file` is ignored on resume.

      * a frozen copy present in the run-dir ⇒ external run: run it, bind its digest;
      * absent ⇒ in-tree run: the in-tree sentinel.

    This is only the RESOLUTION. The tamper check is `verify_identity`: the resolved
    `gate_binding_digest` is fed into the recomputed identity, so a tampered frozen
    gate (digest ≠ committed) OR a deleted frozen copy that flips an external run to
    the in-tree sentinel (≠ the committed external digest) is refused fail-closed at
    `load_and_verify` — a downgrade cannot pass by editing run_inputs alone."""
    from . import checkpoint as ck
    from . import station_run as sr
    frozen = Path(resume_dir) / _FROZEN_GATE_NAME
    if frozen.is_file():
        return str(frozen), sr.gate_digest_of_bytes(frozen.read_bytes())
    return None, ck.INTREE_GATE_BINDING


# Ruling 002 R1: the exit status of a run stopped by SIGTERM/SIGINT before its
# commit boundary. No other path exits with it.
ABORT_EXIT_STATUS = 3


class _Aborted(BaseException):
    """Raised on the loop thread when a latched signal stops the run before the
    commit boundary. BaseException so no `except Exception` swallows it."""


class _Lifecycle:
    """The process-wide abort latch (ruling 002 R1) and the run's progress
    writer (R2). The commit boundary is entry to `finalizing`: a signal latched
    before it stops the run; one received after it is only recorded."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.signal: str | None = None
        self.late = False
        self.interrupted_phase: str | None = None
        self.committed = False
        self._late_recorded = False
        self.task = None
        self.loop = None
        self.progress = None

    def on_signal(self, signum, _frame) -> None:
        # Runs on the main (loop) thread between bytecodes. A repeated signal
        # sets nothing new, so it cannot interrupt a teardown in progress.
        if self.signal is not None:
            return
        self.signal = signal.Signals(signum).name
        if self.committed:
            self.late = True
            return
        self.interrupted_phase = (self.progress.phase if self.progress is not None
                                  else "starting")
        task, loop = self.task, self.loop
        if task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # the loop already closed; the boundary check stops the run

    def check(self) -> None:
        if self.signal is not None and not self.late:
            raise _Aborted

    def commit(self) -> None:
        """Cross the commit boundary, or raise if a signal was latched before it.
        `committed` is set FIRST so a signal arriving during this check is late."""
        self.committed = True
        self.check()
        if self.progress is not None:
            self.progress.finalizing()

    def settle(self) -> bool:
        """Close the latch at the end of the run, on EVERY exit path (a startup
        refusal, a crash, a failed cleanup): True if a signal was latched before
        the commit boundary, so the run is aborted. Any later signal is late."""
        self.committed = True
        return self.signal is not None and not self.late

    def amend_late(self) -> None:
        """Record a late signal the terminal `done` record missed."""
        if self.progress is None or not self.late or self._late_recorded:
            return
        self.progress.amend_terminal(late_signal=self.signal)
        self._late_recorded = True

    def finish(self, aborted: bool) -> None:
        if self.progress is None:
            return
        if aborted:
            self.progress.finish("aborted", signal=self.signal,
                                 interrupted_phase=self.interrupted_phase)
        else:
            # Read when the record is written, not now: a first signal landing
            # while the heartbeat is joined or the record written is still in it.
            def fields():
                if self.late:
                    self._late_recorded = True
                    return {"late_signal": self.signal}
                return {}
            self.progress.finish("done", fields=fields)


_LIFE = _Lifecycle()


def _start_progress(run_dir: str, cfg: HarnessConfig, spec: str,
                    acceptance: str) -> None:
    """Start `progress.json` and its heartbeat for a run with a run directory,
    before the run's worktree is created (ruling 002 R2)."""
    from . import progress
    *_, manifest_id = _run_identity(cfg, spec, acceptance)
    limits = {**_effective_config_dict(cfg), "c_max": cfg.timeouts.c_max(),
              "n_max": cfg.n_max()}
    _LIFE.progress = progress.ProgressWriter(run_dir, run_manifest_id=manifest_id,
                                             limits=limits)
    _LIFE.progress.start()


def _run_feature(main):
    """`asyncio.run(main())` under the abort latch, then cross the commit
    boundary. A signal latched at any point before the boundary raises _Aborted,
    so finalization never starts."""
    async def _guarded():
        _LIFE.task, _LIFE.loop = asyncio.current_task(), asyncio.get_running_loop()
        try:
            _LIFE.check()
            return await main()
        finally:
            _LIFE.task = None

    _LIFE.check()
    try:
        result = asyncio.run(_guarded())
    except (asyncio.CancelledError, _Aborted):
        if _LIFE.signal is None:
            raise
        raise _Aborted from None
    _LIFE.commit()
    return result


def cmd_fix(args):
    """Run `_cmd_fix` and remove the run's worktree afterwards on EVERY path --
    pass, escalation, refusal (SystemExit), crash, Ctrl-C or SIGTERM. A signal
    before the commit boundary exits ABORT_EXIT_STATUS after that removal."""
    run = _RunWorktree()
    _LIFE.reset()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _LIFE.on_signal)
        except ValueError:
            pass  # not the main thread: no handler can be installed
    error = None
    try:
        try:
            _cmd_fix(args, run)
        finally:
            run.remove()
    except _Aborted:
        pass
    except BaseException as e:
        # A startup refusal, a crash or a failed cleanup: a signal latched before
        # it still makes this an abort, so the latch decides below.
        error = e
    try:
        aborted = _LIFE.settle()
        _LIFE.finish(aborted)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    # A first signal can land after finish() last read its fields and before the
    # handlers above were restored. The handler can only set the flag (it must not
    # take the writer's lock), so the record is amended here, in ordinary flow,
    # once no further signal can reach it (codex review r1 MAJOR).
    _LIFE.amend_late()
    if aborted:
        if error is not None and not isinstance(error, SystemExit):
            print(f"potluck: while stopping: {type(error).__name__}: {error}",
                  file=sys.stderr)
        print(f"potluck: stopped by {_LIFE.signal} during {_LIFE.interrupted_phase}; "
              f"nothing was published or delivered.", file=sys.stderr)
        sys.exit(ABORT_EXIT_STATUS)
    if error is not None:
        raise error


# A model reply is unbounded; persisting it verbatim is a storage and
# serialization risk on the FAILURE path, where the run must still exit the way
# it declared. 256 KiB keeps any plausible reply whole while bounding the write.
_REPLY_PERSIST_MAX = 256 * 1024


def _warn_quietly(msg: str) -> None:
    """Report a non-fatal problem without any chance of altering the exit.

    The handlers that call this exist so a persistence failure cannot replace
    the run's declared outcome -- and a bare `print` to stderr can itself raise
    (a closed or broken pipe), reintroducing exactly that (codex MAJOR r2,
    `capture-warning-can-alter-outcome`). A reporting path that can crash is not
    a reporting path."""
    try:
        print(f"Warning: {msg}; the run's outcome is unaffected.",
              file=sys.stderr)
    except Exception:                            # noqa: BLE001 -- last resort
        pass


def _persist_reply_sidecar(args, log_out) -> None:
    """Write the no-change reply beside the checkpoint when --run-dir was given,
    so recovery does not depend on the optional --log-file; if NEITHER artifact
    was requested, say on stderr that the reply was not persisted and how to
    capture it next time. Operator-only (0o600) for the same reason the log is:
    the reply can quote the operator's source."""
    import json as _json
    run_dir = getattr(args, "run_dir", None) or getattr(args, "resume", None)
    if not run_dir:
        if not getattr(args, "log_file", None):
            print("Note: the doer's full reply was NOT persisted -- neither "
                  "--log-file nor --run-dir was given. Re-run with one of them "
                  "to capture it.", file=sys.stderr)
        return
    path = Path(run_dir) / "doer-no-change-reply.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        _json.dump({k: v for k, v in log_out.items()
                    if k.startswith("doer_no_change_reply")}, f, indent=2)
    print(f"  Doer's no-change reply: {path}", file=sys.stderr)


def _restore_scope(worktree, allow_dirty: bool = False) -> str:
    """What the run's restore covers, stated on EVERY run's output. Disclosure
    only: the run cannot enumerate repos it was never told about, so it neither
    restores nor detects edits outside its own worktree -- and says so, so
    "nothing was restored elsewhere" is a recorded fact, not an assumption.

    Takes the WORKTREE, not the operator's checkout (codex MAJOR r1,
    `restore-scope-wrong-path`): naming `run.root` told the reader the restore
    covered the checkout it specifically does NOT touch -- the exact
    claims-coverage-it-lacks defect this function exists to end, committed by
    the function itself.

    `--allow-dirty` sets `restore_tree = None`, so there is NO restore at all
    (codex MAJOR r1, `RESTORE-SCOPE-ALLOW-DIRTY`). Saying "only this worktree"
    would then overstate by claiming a restore that was never wired."""
    if allow_dirty:
        return ("NOTHING -- --allow-dirty disables tree restore entirely. No "
                "path is restored, and edits anywhere (including this run's "
                "worktree) are neither restored nor detected by this run")
    where = worktree or "this run's worktree"
    return (f"only this run's own worktree ({where}); edits made anywhere else "
            f"-- another repo, the operator's checkout, or any path outside "
            f"that worktree -- are neither restored nor detected by this run")


def _prepare_export_dir(export_dir: str, run_dir: str | None,
                        task_id: str | None) -> None:
    """Validate an --export-dir before the run starts (exits on refusal): it needs a
    --run-dir (where station-run.json is emitted) and an --export-task (the manifest
    names its task), must live OUTSIDE the operator's git tree, and any stale manifest
    is removed so no earlier export survives as this run's result."""
    from . import export as ex
    if not run_dir:
        print("Error: --export-dir requires --run-dir (the exported station-run.json "
              "is emitted there).", file=sys.stderr)
        sys.exit(1)
    if not task_id:
        print("Error: --export-dir requires --export-task (the export manifest names "
              "the task it belongs to).", file=sys.stderr)
        sys.exit(1)
    d = Path(export_dir).resolve()
    root = _git_toplevel()
    if d == root or root in d.parents:
        print(f"Error: --export-dir ({d}) is INSIDE the git working tree ({root}). "
              f"An export must live OUTSIDE the tree it describes.", file=sys.stderr)
        sys.exit(1)
    try:
        ex.clear_export(d)
    except OSError as e:
        print(f"Error: cannot clear a stale export manifest in {d}: {e}",
              file=sys.stderr)
        sys.exit(1)


def _export(run: _RunWorktree, export_dir: str, run_dir: str, spec: str,
            acceptance: str, gate_bytes: bytes | None, task_id: str,
            round_index: int,
            imported_result_digest: str | None = None) -> tuple[str | None, str | None]:
    """Write a PASSED run's export. Returns (error, manifest digest); on error no
    manifest is published, so the directory is not acceptable. An importing run
    passes the MANIFEST digest of the result it imported, chaining the rounds."""
    import uuid
    from . import checkpoint as ck
    from . import export as ex
    from . import station_run as sr
    err, tree, patch = run.change()
    if err is not None:
        return err, None
    try:
        if gate_bytes is None:
            raise OSError("the run's gate bytes were not captured")
        roots = run._git("rev-list", "--max-parents=0", run.base, cwd=str(run.root))
        if roots.returncode != 0:
            raise OSError(f"cannot read the repository's root commits: "
                          f"{roots.stderr.decode()[:300]}")
        # codex r1 MAJOR: a basename plus root commits cannot distinguish a repository
        # from a FORK checked out under the same directory name, and part 3's delivery
        # must check the EXPECTED repository. So the manifest records the raw push URL
        # exactly as git reports it, labelled raw. It is deliberately NOT canonicalised
        # here: the fleet's one canonicaliser (host/owner/repo, https-only, port-refusing)
        # lives in the conductor's push_safety, which potluck does not depend on, and a
        # second copy of a security-relevant parser is the drift reviewgate is vendored
        # byte-identically to prevent. The consumer canonicalises with that one
        # implementation; the station reports what it observed.
        pushed = run._git("remote", "get-url", "--push", "origin", cwd=str(run.root))
        push_url = (pushed.stdout.decode().strip()
                    if pushed.returncode == 0 else None)
        digest = ex.write_export(
            Path(export_dir), task_id=task_id, run_id=uuid.uuid4().hex,
            round_index=round_index, imported_result_digest=imported_result_digest,
            repository={"name": run.root.name,
                        "push_url_raw": push_url,
                        "root_commits": sorted(roots.stdout.decode().split())},
            base_commit=run.base, output_tree_oid=tree,
            output_tree_digest=ck._tree_digest(run.path, tree), patch=patch,
            station_run_bytes=(Path(run_dir) / sr.STATION_RUN_FILENAME).read_bytes(),
            spec=spec, acceptance=acceptance, gate_bytes=gate_bytes)
    except (OSError, ck.CheckpointError, ex.ExportError) as e:
        # ExportError too (codex r2 MINOR): `_write_atomic` raises it when a temp
        # NAME cannot be cleared -- e.g. `cumulative.patch.tmp` is a directory --
        # and it is not an OSError, so it escaped as a traceback instead of the
        # promised export_failed JSON and human summary.
        return f"could not write the export to {export_dir}: {e}", None
    return None, digest


# Written into an importing run's run-dir, so a later `--resume` of it is refused by
# name even when --import-dir is not passed again.
_IMPORT_MARKER_NAME = "imported-result.json"


def _write_nofollow(path: Path, data: bytes) -> None:
    """Write `data` to `path`, following NO symlink at that name: the name is unlinked
    (removing the LINK, never its target) and recreated O_CREAT|O_EXCL|O_NOFOLLOW."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _overlaps(a: str, b: str) -> bool:
    """True if resolved paths `a` and `b` are the same, or one contains the other."""
    pa, pb = Path(a).resolve(), Path(b).resolve()
    return pa == pb or pa in pb.parents or pb in pa.parents


def _refuse_links_into_import(import_dir: str, outputs: "dict[str, str | None]") -> None:
    """Refuse a write destination that REACHES the import through a symlink, even when
    the directories are disjoint (codex r1 MAJOR). `_overlaps` compares paths, so a
    pre-created `run1/imported-result.json -> export0/station-run.json` passed it, and
    the marker write then followed the link and truncated the accepted input artifact
    before the doer ran. Every existing entry under a destination is checked; the writes
    this slice adds are also made no-follow, so this is the outer of two defences."""
    imp = Path(import_dir).resolve()
    for flag, dest in outputs.items():
        if dest is None:
            continue
        d = Path(dest)
        if not d.exists() and not d.is_symlink():
            continue
        candidates = [d] + (sorted(d.rglob("*")) if d.is_dir() else [])
        for entry in candidates:
            if not entry.is_symlink():
                continue
            target = Path(os.path.realpath(entry))
            if target == imp or imp in target.parents:
                print(f"Error: {flag} contains a symlink that reaches the import "
                      f"({entry} -> {target}); the import is an input and is never "
                      f"written to.", file=sys.stderr)
                sys.exit(1)


def _load_import(import_dir: str, outputs: "dict[str, str | None]"
                 ) -> "tuple[bytes, str, str, str]":
    """Accept an --import-dir before the run starts (exits on refusal). Returns (patch,
    attested output_tree_digest, manifest digest, base_commit). Only READS the directory.

    `outputs` names every destination this run writes (flag -> path or None). Any that
    overlaps the import is refused HERE, before anything is written: checking only
    equality with --export-dir left --run-dir free to pass the outside-the-tree check
    and then write its checkpoint, run inputs, import marker and station-run.json INTO
    the import -- clearing and replacing the previously accepted export's artifact."""
    from . import export as ex
    for flag, dest in outputs.items():
        if dest is not None and _overlaps(dest, import_dir):
            print(f"Error: --import-dir and {flag} overlap ({import_dir} vs {dest}); the "
                  f"import is an input and is never written to.", file=sys.stderr)
            sys.exit(1)
    _refuse_links_into_import(import_dir, outputs)
    # load_export verifies every payload's bytes against the manifest -- but we then
    # READ the patch again, and the bytes we apply must be the bytes that were
    # accepted. codex r1 MAJOR: a substitution between the two reads bypassed the
    # digest check, and the tree-digest argument that used to sit here does NOT close
    # it -- a substituted patch can produce the attested tree PLUS AN IGNORED FILE,
    # which `_output_tree_digest` excludes by design while the doer and the gate can
    # still consume it. `import_restore` would reapply those bytes on every retry too.
    # So the captured bytes are re-verified against the accepted manifest here.
    try:
        manifest, digest = ex.load_export(Path(import_dir))
        patch = (Path(import_dir) / ex.PATCH_FILENAME).read_bytes()
        got = hashlib.sha256(patch).hexdigest()
        want = manifest["payloads"][ex.PATCH_FILENAME]
        if got != want:
            raise ex.ExportError(
                f"{ex.PATCH_FILENAME} changed after the export was accepted "
                f"(manifest {want}, bytes now {got}); nothing was applied")
    except (ex.ExportError, OSError) as e:
        print(f"Error: import refused (fail-closed): {e}", file=sys.stderr)
        sys.exit(1)
    return (patch, manifest["output_tree_digest"], digest, manifest["base_commit"])


def _cmd_fix(args, run: _RunWorktree):
    from . import checkpoint as ck
    # Absolute now: the run executes with cwd inside its own worktree, which is
    # removed afterwards, so a relative run-dir must not resolve against it.
    resume_dir = getattr(args, "resume", None)
    if resume_dir:
        resume_dir = str(Path(resume_dir).resolve())
    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        run_dir = str(Path(run_dir).resolve())
    allow_dirty = getattr(args, "allow_dirty", False)
    # conductor §5: an external gate path (outside the working tree) resolved to
    # ABSOLUTE now, before any chdir, so `bash <gate>` runs the same file regardless
    # of cwd. None keeps the in-tree .claude/verify.sh.
    gate_file = getattr(args, "gate_file", None)
    if gate_file is not None:
        gate_file = str(Path(gate_file).resolve())
    # §4f station-run artifact: the output dir, the base commit the run started from,
    # and the gate digest captured at run START (before the doer, which can rewrite
    # the gate) — emitted after the run completes (below). With --gate-file the digest
    # is of the EXTERNAL gate (the gate actually run), so the artifact + the resume
    # binding both reflect the real gate.
    _art_dir = None
    _art_base = None
    _art_gate = None
    # Ruling 001 part 1: a run told to export writes a portable result here instead
    # of delivering into the operator's tree. Resolved now, before any chdir.
    export_dir = getattr(args, "export_dir", None)
    if export_dir is not None:
        export_dir = str(Path(export_dir).resolve())
    _gate_bytes = None  # the exact gate bytes behind _art_gate, for the export
    # Ruling 001 part 2: a run told to import starts from a previous round's attested
    # output tree instead of committed HEAD. Resolved now, before any chdir.
    import_dir = getattr(args, "import_dir", None)
    if import_dir is not None:
        import_dir = str(Path(import_dir).resolve())
    import_digest = None  # the imported result's manifest digest, chained on export

    if resume_dir:
        # DECIDED: an importing resume is REFUSED by name, not built. A resume restores
        # the checkpoint, but its retries would also need the import re-verified from
        # the run-dir record, and export of a resumed run is not built (below), so a
        # resumed importing run could never chain its result. Refusing is the safe half:
        # a resume that silently restarted from HEAD would discard the previous round.
        if import_dir is not None or (Path(resume_dir) / _IMPORT_MARKER_NAME).exists():
            print("Error: --resume of an importing run is not supported (resume of a "
                  "run that imported an attested result is not built yet); re-run the "
                  "round fresh with --import-dir.", file=sys.stderr)
            sys.exit(1)
        if export_dir is not None:
            print("Error: --export-dir is not supported with --resume (export of a "
                  "resumed run is not built yet).", file=sys.stderr)
            sys.exit(1)
        # --- resume path (hardening Step-0 §2): spec/acceptance come from the
        #     persisted run inputs, NOT --spec; the identity is re-verified
        #     fail-closed and the tree restored losslessly before re-entry. ---
        try:
            inputs = ck.load_run_inputs(resume_dir)
        except ck.CheckpointError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        spec, acceptance = inputs["spec"], inputs["acceptance"]
        cfg = load_config(args)
        _apply_timeout_overrides(args, cfg)
        # Gate (conductor §5): resolved from a FILE FACT — the frozen copy's presence —
        # so an external run resumes from its frozen copy (never the mutable in-tree
        # gate) and an in-tree run resumes in-tree. `--gate-file` is ignored on resume.
        # The gate_binding_digest flows into the recomputed identity, so a tampered
        # frozen gate or a deleted-frozen downgrade is refused at load_and_verify below.
        gate_path, gate_binding = _resolve_resume_gate(resume_dir)
        if gate_path is None:
            _require_gate(None)  # in-tree run: the in-tree gate must still be present
            _art_gate = _digest_gate_file(".claude/verify.sh")
        else:
            _art_gate = gate_binding  # external: the frozen copy's digest (the run gate)
        # The operator's tree must be clean (see ensure_clean_tree). The lossless
        # restore itself now lands in the run's fresh worktree, detached at the
        # operator's HEAD, so restore_tree's own "HEAD is not the checkpoint
        # base" refusal still fires when the operator's branch moved.
        dirty_err = ensure_clean_tree(allow_dirty)
        if dirty_err:
            print(f"Error: {dirty_err}", file=sys.stderr)
            sys.exit(1)
        _refuse_run_dir_inside_tree(resume_dir)  # against the OPERATOR's tree
        _start_progress(resume_dir, cfg, spec, acceptance)
        run.enter()
        _LIFE.progress.set(worktree=str(run.path))
        checkpointer, *_ = _build_checkpointer(resume_dir, cfg, spec, acceptance,
                                               gate_binding)
        try:
            resume_ckpt = checkpointer.load_and_verify()
            checkpointer.restore(resume_ckpt.tree)
        except ck.CheckpointError as e:
            print(f"Error: resume refused (fail-closed): {e}", file=sys.stderr)
            sys.exit(1)
        _art_dir = resume_dir
        _art_base = resume_ckpt.tree.repo_base_commit
        from . import station_run as _sr
        _sr.clear_station_run_artifact(_art_dir)  # no stale artifact survives a run
        print(f"potluck: RESUMING from {resume_dir} at "
              f"{resume_ckpt.cursor.phase.value} (round {resume_ckpt.cursor.round_index})")
        print(f"spec: {spec[:80]}{'...' if len(spec) > 80 else ''}\n")

        async def _run():
            orch = build_orchestrator(cfg, allow_dirty=allow_dirty,
                                      checkpointer=checkpointer, gate_path=gate_path,
                                      progress=_LIFE.progress)
            return await orch.run_feature(spec, acceptance, resume=resume_ckpt)

        result = _run_feature(_run)
    else:
        # --- fresh path (optionally checkpointed via --run-dir) ---
        if args.spec_file:
            spec = Path(args.spec_file).read_text().strip()
        elif args.spec:
            spec = args.spec
        else:
            print("Error: provide --spec or --spec-file", file=sys.stderr)
            sys.exit(1)
        acceptance = (Path(args.acceptance_file).read_text().strip()
                      if args.acceptance_file else args.acceptance)

        cfg = load_config(args)
        _apply_timeout_overrides(args, cfg)
        # conductor §5: an external gate is a station-run mechanism — it requires a
        # run-dir (the frozen copy's durable, resumable home) and must live OUTSIDE the
        # working tree. Both are validated before the gate is required/frozen.
        if gate_file is not None:
            if not run_dir:
                print("Error: --gate-file requires --run-dir (the external gate is "
                      "frozen into the run-dir so a resume re-runs the same gate).",
                      file=sys.stderr)
                sys.exit(1)
            _refuse_gate_inside_tree(gate_file)
        if import_dir is not None and export_dir is None:
            # Ruling 001 part 3: an importing run delivering directly would be a THIRD
            # delivery path, landing an imported cumulative candidate with none of
            # delivery's safeguards. Refused before anything runs.
            print("Error: --import-dir requires --export-dir. An importing run does not "
                  "deliver into your tree: run the round with --import-dir and "
                  "--export-dir, then deliver the accepted result with `potluck deliver "
                  "--export-dir <dir> --manifest-digest <digest>`.", file=sys.stderr)
            sys.exit(1)
        if import_dir is not None:
            log_file = getattr(args, "log_file", None)
            import_patch, import_tree_digest, import_digest, import_base = _load_import(
                import_dir, {"--run-dir": run_dir, "--export-dir": export_dir,
                             "--log-file": log_file and str(Path(log_file).resolve())})
        if export_dir is not None:
            _prepare_export_dir(export_dir, run_dir, getattr(args, "export_task", None))
        _require_gate(gate_file)
        # Clean-tree precondition (before the banner): a dead implement attempt
        # leaves partial edits behind, so we start from a known-clean baseline
        # restore_tree can reset to. --allow-dirty is the visible opt-out.
        dirty_err = ensure_clean_tree(allow_dirty)
        if dirty_err:
            print(f"Error: {dirty_err}", file=sys.stderr)
            sys.exit(1)
        if run_dir:
            # Validate against the OPERATOR's tree, BEFORE freezing into it.
            _refuse_run_dir_inside_tree(run_dir)
            _start_progress(run_dir, cfg, spec, acceptance)
        run.enter()
        if _LIFE.progress is not None:
            _LIFE.progress.set(worktree=str(run.path))
        import_restore = None
        if import_dir is not None:
            # In the fresh worktree at the operator's HEAD, before the doer and before
            # any checkpoint snapshot -- so every snapshot carries the imported state.
            import_err = run.apply_import(import_patch, import_tree_digest,
                                          import_base)
            if import_err is not None:
                print(f"Error: import refused (fail-closed): {import_err}",
                      file=sys.stderr)
                sys.exit(1)
            import_restore = run.import_restore(import_patch, import_tree_digest,
                                                import_base)

        checkpointer = None
        gate_path = None
        if run_dir:
            # An external gate is FROZEN into the (outside-the-tree) run-dir and the
            # IMMUTABLE copy is run: the digest is provably the executed bytes, a resume
            # re-runs the same gate, and gate_binding is bound into the tamper-evident
            # identity (codex §5 r1/r2 MAJOR/BLOCKING). In-tree uses the sentinel (its
            # bytes are already bound via the tree snapshot). Captured BEFORE the doer
            # runs (§4f — the doer can rewrite .claude/verify.sh).
            if gate_file is not None:
                gate_path, gate_binding = _freeze_gate(gate_file, run_dir)
                _art_gate = gate_binding
                if export_dir is not None:
                    _gate_bytes = Path(gate_path).read_bytes()
            else:
                gate_binding = ck.INTREE_GATE_BINDING
                _art_gate = _digest_gate_file(".claude/verify.sh")
                if export_dir is not None:
                    _gate_bytes = Path(".claude/verify.sh").read_bytes()
            checkpointer, cfg_digest, base, manifest_id = _build_checkpointer(
                run_dir, cfg, spec, acceptance, gate_binding)
            ck.persist_run_inputs(run_dir, spec, acceptance, cfg_digest, base,
                                  manifest_id)
            marker = Path(run_dir) / _IMPORT_MARKER_NAME
            if import_digest is not None:
                # No-follow (codex r1 MAJOR): a planted link here pointed into the
                # import and `write_text` followed it, truncating the accepted input.
                _write_nofollow(marker, json.dumps(
                    {"imported_result_digest": import_digest,
                     "import_dir": import_dir}, indent=2).encode())
            else:
                marker.unlink(missing_ok=True)  # a reused run-dir's stale marker
            _art_dir = run_dir
            _art_base = base
            from . import station_run as _sr
            _sr.clear_station_run_artifact(_art_dir)  # no stale artifact survives a run

        print(f"potluck: profile={cfg.profile}, debate={'on' if cfg.debate_enabled else 'off'}, "
              f"max_rounds={cfg.debate.max_rounds}"
              + (f", checkpoint={run_dir}" if run_dir else ""))
        print(f"roles: reviewer={cfg.models.reviewer.name}, tiebreaker={cfg.models.tiebreaker.name}")
        print(f"spec: {spec[:80]}{'...' if len(spec) > 80 else ''}\n")

        async def _run():
            orch = build_orchestrator(cfg, allow_dirty=allow_dirty,
                                      checkpointer=checkpointer, gate_path=gate_path,
                                      import_restore=import_restore,
                                      progress=_LIFE.progress)
            return await orch.run_feature(spec, acceptance)

        result = _run_feature(_run)

    # §4f: emit the station-run artifact (the producer half of the station-provenance
    # chain) for a STATION RUN — one with an out-of-tree run directory (--run-dir or
    # --resume). The conductor always drives `potluck fix` with --run-dir (§5), so every
    # conductor-driven run emits one; a bare local `fix` without --run-dir has no
    # out-of-tree home for the artifact (writing inside the worktree would pollute the
    # very tree being digested) and is not a persisted station run. Attests the run's
    # inputs, base, terminal state, and OUTPUT tree. Best-effort here (provenance, not a
    # gate; a failure warns, never fails an otherwise-complete run) — the later required
    # reviewgate station_run field fails CLOSED on a missing artifact, so a station run
    # whose artifact never landed cannot authorize a product push (codex slice-2).
    if _art_dir is not None:
        from . import station_run as sr
        try:
            artifact = sr.build_station_run_artifact(
                _git_toplevel(), spec, acceptance, _art_gate,
                _art_base, result.outcome.value)
            sr.write_station_run_artifact(Path(_art_dir), artifact)
        except Exception as e:  # noqa: BLE001 — provenance emission is best-effort
            print(f"warning: station-run artifact not written: {e}", file=sys.stderr)

    # Deliver ONLY a passed run's change into the operator's tree (see
    # _RunWorktree: on failure nothing is written there). The artifact above
    # attested the run's own output tree -- the worktree -- before delivery.
    #
    # A run told to EXPORT is never delivered: on PASSED its result is written to
    # the export directory instead, and a passed run whose export cannot be
    # completed fails (the export is the round's product).
    export_digest = None
    if export_dir is not None:
        delivery_err = None
        if result.outcome.value == "passed":
            delivery_err, export_digest = _export(
                run, export_dir, run_dir, spec, acceptance, _gate_bytes,
                args.export_task, getattr(args, "export_round", 0), import_digest)
        run.leave()
        if delivery_err:
            print(f"Error: run passed but its result was NOT exported: {delivery_err}",
                  file=sys.stderr)
    else:
        delivery_err = (run.deliver() if result.outcome.value == "passed" else None)
        run.leave()
        if delivery_err:
            print(f"Error: run passed but its change was NOT delivered: {delivery_err}",
                  file=sys.stderr)

    out = {
        "outcome": result.outcome.value,
        "rounds_used": result.rounds_used,
        "escalation_reason": result.escalation_reason,
        # Harness-verified class-closure sweep (None unless the run PASSED and
        # the sweep found sibling candidates). JSON mode carries it here; the
        # human-readable section below is printed only in non-JSON mode.
        "closure_report": (result.closure_report.model_dump(mode="json")
                           if result.closure_report else None),
        "restore_scope": _restore_scope(run.path, getattr(args, "allow_dirty", False)),
        "debate_log": result.debate_log,
    }
    # An export run reports its export explicitly. A PASSED station whose export
    # failed is NOT a passed run: `outcome` says export_failed, and the station's own
    # verdict is kept separately in `station_outcome`.
    if export_dir is not None:
        out["station_outcome"] = result.outcome.value
        if delivery_err:
            out["outcome"] = "export_failed"
            out["export_status"] = "failed"
            out["export_error"] = delivery_err
        else:
            out["export_status"] = ("exported" if export_digest is not None
                                    else "not_exported")
        if export_digest is not None:
            out["export_manifest_digest"] = export_digest

    if not (hasattr(args, "json_output") and args.json_output):
        outcome = out["outcome"]
        if outcome == "passed":
            symbol = "PASSED"
        elif outcome == "export_failed":
            symbol = "FAILED (station passed, export failed)"
        else:
            symbol = f"ESCALATED ({outcome})"
        print(f"\n{'='*60}")
        print(f"  {symbol}  |  Rounds: {out['rounds_used']}")
        if out["escalation_reason"]:
            print(f"  Reason: {out['escalation_reason']}")
        if out.get("export_error"):
            print(f"  Export: NOT exported: {out['export_error']}")
        elif out.get("export_manifest_digest"):
            print(f"  Export manifest: {out['export_manifest_digest']}")
        print(f"  Restore scope: {out['restore_scope']}")
        print(f"{'='*60}")
        # Class-closure sweep: only on a PASSED run that found siblings. The
        # presentation keeps the two kinds of text unmissably distinct so a
        # path-shaped string the model wrote can't be mistaken for a finding:
        #   * bug_class is the MODEL'S words -- unverified explanation, labelled
        #     `model-supplied` and never rendered as a location;
        #   * candidates are HARNESS-VERIFIED grep matches -- real file:line hits
        #     the harness found, shown under a heading that says so. They are
        #     still candidates to JUDGE, not confirmed bugs.
        # `rationale` is deliberately NOT printed here (it adds no operator value
        # next to the pattern and is one more unverified string on screen); it
        # stays in --json-output for structured consumers that can tell the keys
        # apart.
        cr = out["closure_report"]
        if result.outcome.value == "passed" and cr and cr["candidates"]:
            print("\nCLASS-CLOSURE SWEEP")
            print(f"  Bug class (model-supplied, unverified): {cr['bug_class']}")
            print("  Harness-verified grep matches -- candidates for you to "
                  "judge, NOT confirmed bugs:")
            for c in cr["candidates"]:
                print(f"    {c['file']}:{c['line']}: {c['text'].strip()}")
                print(f"        (matched pattern: {c['pattern']})")
        if out["debate_log"]:
            print("\nDebate log:")
            for entry in out["debate_log"]:
                print(f"  [{entry['event']}] "
                      f"{json.dumps({k: v for k, v in entry.items() if k != 'event'})}")
    else:
        print(json.dumps(out, indent=2, default=str))

    # The doer's COMPLETE no-change reply is persisted to operator-only (0o600)
    # artifacts. stdout keeps the debate log's capped preview -- note that a
    # reply SHORTER than the cap is complete in that preview too, so the claim
    # is "nothing longer than the cap reaches stdout", not "stdout never holds a
    # complete reply" (codex MINOR r1, `NOCHANGE-STDOUT-CONFINEMENT`).
    #
    # RECOVERY MUST NOT DEPEND ON AN OPTIONAL FLAG (codex MAJOR r1,
    # `no-change-recovery-optional`): A1 requires the reply be recoverable after
    # a no-change run, and --log-file is optional. It is written to the --run-dir
    # sidecar as well when there is one, and when NEITHER artifact was requested
    # the run says so on stderr instead of silently dropping it -- "not
    # persisted, and here is how to get it" beats a promise the flags did not
    # make.
    #
    # The write is BOUNDED and CANNOT change termination (codex MAJOR r1,
    # `unbounded-reply-can-alter-termination`; A3): an uncapped model reply can
    # fail on serialization or storage, and an exception here would replace the
    # declared no-signal exit with a crash. Capped, and wrapped so any failure
    # is reported and the exit code is unchanged.
    log_out = out
    reply = result.doer_no_change_reply
    if reply is not None:
        capped = reply[:_REPLY_PERSIST_MAX]
        truncated = len(reply) > _REPLY_PERSIST_MAX
        log_out = {**out, "doer_no_change_reply": capped,
                   "doer_no_change_reply_truncated": truncated,
                   "doer_no_change_reply_full_length": len(reply)}
    try:
        write_log(log_out, args)
    except Exception as e:                       # noqa: BLE001 -- never alter the exit
        _warn_quietly(f"could not write the run log ({type(e).__name__}: {e})")
    if reply is not None:
        try:
            _persist_reply_sidecar(args, log_out)
        except Exception as e:                   # noqa: BLE001 -- never alter the exit
            _warn_quietly(f"could not persist the doer's reply "
                          f"({type(e).__name__}: {e})")
    sys.exit(0 if out["outcome"] == "passed" and not delivery_err else 1)


# ---------------------------------------------------------------------------
# deliver — apply ONE exported result, selected by manifest digest (ruling 001 part 3)
# ---------------------------------------------------------------------------

def add_deliver_parser(subparsers):
    p = subparsers.add_parser(
        "deliver",
        help="Apply ONE exported result, selected by its manifest digest, to this "
             "checkout. Runs no doer, no gate and no review.")
    p.add_argument("--export-dir", type=str, required=True,
                   help="The directory holding the exported result (`potluck fix "
                        "--export-dir`). Only read; it locates the result.")
    p.add_argument("--manifest-digest", type=str, required=True,
                   help="The sha256 of the result's export-manifest.json -- the "
                        "result's IDENTITY. A directory whose manifest has any other "
                        "digest is refused: a pathname alone never authorizes delivery.")
    p.add_argument("--json-output", action="store_true")
    p.set_defaults(func=cmd_deliver)


class _DeliveryRefused(Exception):
    """A structured refusal: `kind` says WHICH safeguard refused, `expected`/`observed`
    are the captured facts. Raised only before anything is written to the checkout."""

    def __init__(self, kind: str, detail: str, expected: dict | None = None,
                 observed: dict | None = None):
        super().__init__(detail)
        self.kind, self.detail = kind, detail
        self.expected, self.observed = expected or {}, observed or {}


_REFUSAL_LABEL = {
    "selection": "the selected result is not acceptable",
    "identity": "REPOSITORY IDENTITY mismatch",
    "baseline_drift": "BASELINE DRIFT -- this checkout is not the clean base the result "
                      "was built against",
    "result_invalid": "the result does not reproduce its own attested output",
    "ignored_obstruction": "IGNORED-PATH OBSTRUCTION -- applying would overwrite an "
                           "ignored path",
    "patch_conflict": "PATCH CONFLICT -- the checkout matches the baseline, yet the "
                      "patch does not apply",
    "partial_application": "PARTIAL APPLICATION -- git apply wrote some files and then "
                           "failed; its write phase is not transactional",
}


def _deliver_selected(root: Path, export_dir: Path, digest: str) -> dict:
    """Apply the result at `export_dir` whose manifest digest is `digest` to the operator
    checkout at `root`, ONCE, or raise `_DeliveryRefused`.

    A refusal writes nothing on every path EXCEPT one, which is named rather than
    papered over (codex r2 MINOR): `git apply`'s write phase is not transactional, so a
    failure during it can leave files written. That case refuses as
    `partial_application`, restores to the verified baseline where it safely can, and
    reports the digests and whether the restore succeeded. "All-or-nothing" therefore
    describes `git apply`'s applicability CHECK plus this recovery, not an atomic
    filesystem write that git does not provide.

    THE CONTRACT. Delivery runs no doer, no gate and no review. Every safeguard is
    checked before anything is written, in this order:

      1. SELECTION -- the directory is accepted by `load_export` (fail-closed) and its
         manifest digest equals `digest`; the patch bytes applied are the accepted bytes.
      2. IDENTITY -- `git remote get-url --push origin` and the root commits equal the
         manifest's `push_url_raw` and `root_commits` EXACTLY. The raw URL carries the
         weight (a fork shares the root commits); the roots bind against
         delete-and-recreate, URL reuse and history replacement. A null `push_url_raw`
         is refused: equality against null is vacuous. `repository.name` is never
         compared -- a checkout can be renamed. This is conservative evidence
         continuity, NOT canonical destination comparison (that is the caller's job,
         with the fleet's one canonicaliser): equivalent URL spellings (`.git`,
         `user@`) can cause a FALSE refusal, which the operator can recover from.
      3. THE CAPTURED CLEAN BASELINE -- the clean checkout of `base_commit`, in three
         parts, none optional: HEAD equals it (the tree half cannot see an empty
         commit); the REAL index represents its tree, read without refreshing or
         rewriting it (a temp-index digest cannot see staged changes the working tree
         reverses); and the working tree digests, on the station's add-A boundary
         (untracked-non-ignored in, ignored out), to `base_commit^{tree}`. Patch
         applicability is NOT whole-tree equality: an unrelated foreign edit lets the
         patch apply and leaves a tree the station never attested -- so it is refused.
         An `--allow-dirty` export is therefore deliverable only onto the clean base.
      4. The patch reproduces the attested `output_tree_digest` from `base_commit`,
         checked in a temp index; and no IGNORED path the change touches exists here
         (ignored files are outside the digest boundary, which is not authorization
         to clobber them).
      5. `git apply --binary`: working tree only. No ref is written and the real index
         is never used as scratch. On failure the checkout is re-verified rather than
         assumed clean, and a partial write is named and restored (see above).

    A refusal RETAINS the result -- the export directory is the copy, and it is only
    read -- and is never followed by a rebase or an overwrite.

    WHAT "ONCE" MEANS. A digest-bound application with a baseline refusal: a second
    delivery of the same result refuses, because the checkout is no longer the clean
    base. Durable exactly-once acceptance across retries, crashes, empty patches (which
    deliver as a no-op and leave the baseline intact) or two different results for one
    task is NOT promised here -- that is the caller's orchestration, or a future
    delivery-receipt contract."""
    import subprocess
    import tempfile
    from . import checkpoint as ck
    from . import export as ex
    from . import station_run as sr

    def git(*args, env=None, data=None):
        # GIT_OPTIONAL_LOCKS=0: no opportunistic index refresh may write the real index.
        return subprocess.run(["git", *args], cwd=str(root), input=data,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})},
                              capture_output=True)

    # 1. Selection.
    try:
        manifest, got = ex.load_export(export_dir)
        patch = (export_dir / ex.PATCH_FILENAME).read_bytes()
    except (ex.ExportError, OSError) as e:
        raise _DeliveryRefused("selection", f"the export is refused (fail-closed): {e}")
    if got != digest:
        raise _DeliveryRefused(
            "selection", "the named directory's manifest is not the selected result; a "
            "pathname alone never authorizes delivery",
            {"manifest_digest": digest}, {"manifest_digest": got})
    want_patch = manifest["payloads"][ex.PATCH_FILENAME]
    if hashlib.sha256(patch).hexdigest() != want_patch:
        raise _DeliveryRefused(
            "selection", f"{ex.PATCH_FILENAME} changed after the export was accepted",
            {"patch_digest": want_patch},
            {"patch_digest": hashlib.sha256(patch).hexdigest()})

    # 2. Identity.
    rec = manifest["repository"]
    want_url, want_roots = rec.get("push_url_raw"), rec.get("root_commits")
    if not isinstance(want_url, str) or not want_url:
        raise _DeliveryRefused(
            "identity", "the manifest records no push_url_raw (origin was unreadable at "
            "export), so the expected repository cannot be checked -- equality against "
            "null is vacuous. Give the repository an origin and re-export the round",
            {"push_url_raw": want_url})
    if (not isinstance(want_roots, list) or not want_roots
            or not all(isinstance(r, str) for r in want_roots)):
        raise _DeliveryRefused("identity", "the manifest records no root_commits",
                               {"root_commits": want_roots})
    pushed = git("remote", "get-url", "--push", "origin")
    roots = git("rev-list", "--max-parents=0", "HEAD")
    observed = {"push_url_raw": (pushed.stdout.decode().strip()
                                 if pushed.returncode == 0 else None),
                "root_commits": (sorted(roots.stdout.decode().split())
                                 if roots.returncode == 0 else None)}
    expected = {"push_url_raw": want_url, "root_commits": sorted(want_roots)}
    if observed != expected:
        raise _DeliveryRefused(
            "identity", "this checkout is not the repository the result was exported "
            "from (exact comparison of recorded evidence; an equivalent URL spelling "
            "is refused too -- canonical comparison is the caller's)", expected, observed)

    # 3. The captured clean baseline: HEAD, the real index, the working tree.
    base = manifest["base_commit"]
    head = git("rev-parse", "--verify", "-q", "HEAD")
    head = head.stdout.decode().strip() if head.returncode == 0 else None
    if head != base:
        raise _DeliveryRefused(
            "baseline_drift", "HEAD is not the result's base_commit", {"head": base},
            {"head": head})
    staged = git("diff-index", "--cached", "--name-status", base)
    if staged.returncode != 0 or staged.stdout.strip():
        raise _DeliveryRefused(
            "baseline_drift", "the real index does not represent base_commit's tree "
            "(staged changes, even ones the working tree reverses)",
            {"index_vs_base": "no differences"},
            {"index_vs_base": (staged.stdout.decode().strip()
                               if staged.returncode == 0
                               else f"unverifiable: {staged.stderr.decode()[:200]}")})
    try:
        base_tree = git("rev-parse", f"{base}^{{tree}}").stdout.decode().strip()
        want_tree = ck._tree_digest(root, base_tree)
        got_tree = sr._output_tree_digest(root)
    except ck.CheckpointError as e:
        raise _DeliveryRefused("baseline_drift", f"the working tree is unverifiable: {e}")
    if got_tree != want_tree:
        raise _DeliveryRefused(
            "baseline_drift", "the working tree (tracked + untracked-non-ignored) is not "
            "base_commit's tree -- whatever changed it, applying would yield a tree the "
            "station never attested, even if the patch applies",
            {"working_tree_digest": want_tree}, {"working_tree_digest": got_tree})

    # 4. The patch reproduces the attested output, and obstructs no ignored path.
    with tempfile.TemporaryDirectory() as td:
        env = {"GIT_INDEX_FILE": os.path.join(td, "index")}
        steps = [git("read-tree", base, env=env)]
        if patch:
            steps.append(git("apply", "--cached", "--binary", "-", env=env, data=patch))
        steps.append(git("write-tree", env=env))
    failed = next((p for p in steps if p.returncode != 0), None)
    out_tree = steps[-1].stdout.decode().strip()
    try:
        out_digest = None if failed else ck._tree_digest(root, out_tree)
    except ck.CheckpointError as e:
        failed, out_digest = None, f"unverifiable: {e}"
    if failed is not None or out_digest != manifest["output_tree_digest"]:
        raise _DeliveryRefused(
            "result_invalid", "the result's patch, applied to base_commit, does not "
            "produce its attested output tree",
            {"output_tree_digest": manifest["output_tree_digest"]},
            {"output_tree_digest": (out_digest if failed is None
                                    else f"patch does not apply to base_commit: "
                                         f"{failed.stderr.decode().strip()[:300]}")})
    touched = [p for p in git("diff-tree", "-r", "--no-renames", "--name-only", "-z",
                              base_tree, out_tree).stdout.decode(
                                  errors="surrogateescape").split("\0") if p]
    ignored = [i.rstrip("/") for i in git(
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"
    ).stdout.decode(errors="surrogateescape").split("\0") if i]
    hits = sorted({i for i in ignored for p in touched
                   if p == i or p.startswith(i + "/") or i.startswith(p + "/")})
    if hits:
        raise _DeliveryRefused(
            "ignored_obstruction", "the change writes where an ignored path exists in "
            "this checkout", {"ignored_paths_in_the_way": []},
            {"ignored_paths_in_the_way": hits})

    # 5. Apply: working tree only. `git apply` checks applicability BEFORE writing, but
    # its write phase is NOT transactional (codex r1 MAJOR, with the upstream sources:
    # `write_out_one_result` / `write_out_results` in apply.c remove and recreate files
    # SEQUENTIALLY and return on a write error without rolling earlier files back). So a
    # nonzero exit does NOT prove nothing was written -- a patch touching two tracked
    # files whose second lives in a readable but non-writable directory passes every
    # baseline check and still mutates the first. Reporting that as "wrote nothing"
    # would be a false assurance in the one message the operator acts on.
    #
    # We can tell the two apart, and recover, precisely BECAUSE the baseline was
    # verified above: the working tree was byte-for-byte `base^{tree}` on the add-A
    # boundary, so there were no untracked-non-ignored files, and any that exist now
    # were created by the partial apply.
    if patch:
        applied = git("apply", "--binary", "-", data=patch)
        if applied.returncode != 0:
            stderr = applied.stderr.decode().strip()[:500]
            try:
                after = sr._output_tree_digest(root)
            except ck.CheckpointError as e:
                raise _DeliveryRefused(
                    "partial_application",
                    "git apply failed and the checkout could not be re-verified, so "
                    "whether anything was written is UNKNOWN -- inspect it before "
                    "retrying", {"working_tree_digest": want_tree},
                    {"git_apply": stderr, "re_verification": f"failed: {e}"})
            if after == want_tree:
                raise _DeliveryRefused(
                    "patch_conflict", "git apply refused the patch, and the checkout is "
                    "verified unchanged afterwards", {"applies": True},
                    {"git_apply": stderr, "working_tree_digest": after})
            # Partial write. Restore to the verified baseline and say what happened.
            #
            # ORDER AND CONDITION ARE LOAD-BEARING (codex r2 MAJOR). `clean -fd` must
            # run ONLY if `checkout-index` succeeded. My premise for cleaning at all was
            # that the baseline check proved there were no untracked-non-ignored files,
            # so anything untracked now is the partial apply's -- but that premise holds
            # only under the ORIGINAL ignore rules. If the partial apply modified
            # `.gitignore` (dropping a rule) and the checkout restore then ALSO fails,
            # `clean -fd` would run under the PARTIALLY APPLIED rules and delete operator
            # files that were legitimately ignored at baseline -- `.env`, `.venv`, the
            # very data-loss footgun `real_restore_tree` refuses `-x` to avoid. Those
            # files need not overlap any patch path. So: no restored ignore rules, no
            # clean, and say the recovery was incomplete.
            checkout = git("checkout-index", "-a", "-f")
            restored = [checkout]
            if checkout.returncode == 0:
                restored.append(git("clean", "-fd", "--quiet"))
            else:
                restored_note = ("checkout-index FAILED, so the cleanup was SKIPPED on "
                                 "purpose: with the ignore rules possibly half-applied, "
                                 "`git clean` could delete files that were ignored at "
                                 "the baseline")
            try:
                final = sr._output_tree_digest(root)
            except ck.CheckpointError as e:
                final = f"unverifiable: {e}"
            raise _DeliveryRefused(
                "partial_application",
                "git apply wrote SOME files and then failed -- its write phase is not "
                "transactional. The checkout was restored to the result's base"
                if final == want_tree else
                "git apply wrote SOME files and then failed -- its write phase is not "
                "transactional, AND the restore did not return the checkout to the "
                "result's base; inspect it before retrying",
                {"working_tree_digest": want_tree},
                {"git_apply": stderr, "working_tree_digest_after_apply": after,
                 "working_tree_digest_after_restore": final,
                 "restore": ("ok" if all(r.returncode == 0 for r in restored)
                             else (restored_note if checkout.returncode != 0
                                   else "the cleanup failed after a successful "
                                        "checkout-index"))})
    return {"task_id": manifest["task_id"], "round": manifest["round"],
            "base_commit": base, "output_tree_digest": manifest["output_tree_digest"],
            "delivered_tree_digest": sr._output_tree_digest(root)}


def cmd_deliver(args):
    """`potluck deliver`: the explicit delivery operation. Never builds an orchestrator,
    so no model is called; see `_deliver_selected` for the contract."""
    import subprocess
    export_dir = Path(args.export_dir).resolve()
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True)
    out = {"manifest_digest": args.manifest_digest, "retained_result": str(export_dir)}
    try:
        if top.returncode != 0:
            raise _DeliveryRefused("identity", "run `potluck deliver` inside the "
                                   "operator's git checkout",
                                   observed={"cwd": os.getcwd()})
        facts = _deliver_selected(Path(top.stdout.strip()), export_dir,
                                  args.manifest_digest)
    except _DeliveryRefused as r:
        out.update(outcome="refused", refusal_kind=r.kind, reason=r.detail,
                   expected=r.expected, observed=r.observed)
        lines = [f"Error: delivery REFUSED -- {_REFUSAL_LABEL.get(r.kind, r.kind)}: "
                 f"{r.detail}."]
        lines += [f"  expected {k}: {v}" for k, v in r.expected.items()]
        lines += [f"  observed {k}: {v}" for k, v in r.observed.items()]
        # The "nothing was written" footer is TRUE for every refusal kind except a
        # partial application, where git's non-transactional write phase had already
        # written files. Printing it there would be the inaccurate assurance this slice
        # exists to remove -- found by the fault-injection test for the skipped cleanup,
        # not by the review. What is known instead is the digests above and whether the
        # restore succeeded.
        if r.kind == "partial_application":
            # `partial_application` covers TWO shapes, and conflating them would restate
            # this slice's own defect in a new place (codex r3, non-blocking): a write
            # that demonstrably happened, and a re-verification that FAILED -- where
            # whether anything was written is genuinely UNKNOWN and there is no current
            # digest to point at. The footer must preserve that uncertainty.
            if "re_verification" in r.observed:
                lines += ["  Whether anything was written is UNKNOWN: the checkout "
                          "could not be re-verified after the failure, so this refusal "
                          "claims nothing about its current state -- inspect it before "
                          "retrying. No ref was written and the index was not used as "
                          "scratch."]
            else:
                lines += ["  Files WERE written before the failure, and the lines above "
                          "say what the checkout digests to now. No ref was written, "
                          "the index was not used as scratch, and there was no rebase "
                          "or overwrite."]
        else:
            lines += ["  Nothing was written: HEAD, every ref, the index and the working "
                      "tree are as they were. No rebase, no overwrite."]
        lines += [f"  The selected result is RETAINED, unchanged, at {export_dir} "
                  f"(manifest digest {args.manifest_digest})."]
        print("\n".join(lines), file=sys.stderr)
        if args.json_output:
            print(json.dumps(out, indent=2))
        sys.exit(1)
    ok = facts["delivered_tree_digest"] == facts["output_tree_digest"]
    out.update(outcome="delivered" if ok else "delivered_unverified", **facts)
    if args.json_output:
        print(json.dumps(out, indent=2))
    elif ok:
        print(f"potluck: delivered result {args.manifest_digest} (task "
              f"{facts['task_id']}, round {facts['round']}) onto {facts['base_commit']} "
              f"as uncommitted change; the tree now matches the attested output "
              f"{facts['output_tree_digest']}. No ref was written; the index is "
              f"untouched.")
    if not ok:
        print(f"Error: the patch WAS applied, but the tree now digests to "
              f"{facts['delivered_tree_digest']}, not the attested "
              f"{facts['output_tree_digest']} -- something changed the checkout during "
              f"delivery. The result is retained at {export_dir}.", file=sys.stderr)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# resolve / doctor — model wiring for this machine
# ---------------------------------------------------------------------------

def add_resolve_parser(subparsers):
    p = subparsers.add_parser("resolve", help="Pick which models fill each role on this machine")
    p.add_argument("--auto", action="store_true", help="Use Codex/Kimi if detected, no prompts")
    p.add_argument("--claude-only", action="store_true", help="Force the Claude-only floor")
    p.set_defaults(func=lambda a: sys.exit(
        resolve_mod.cmd_resolve(auto=a.auto, claude_only=a.claude_only)))


def _reviewgate_doctor() -> int:
    import subprocess
    vendored = Path(__file__).parent / "_vendor" / "reviewgate.py"
    return subprocess.run(["python3", str(vendored), "--doctor"]).returncode


def _doctor_and_gate() -> int:
    # Run BOTH and fail if EITHER fails — never let a gate-doctor failure be
    # swallowed into an overall success (fail-closed reporting; finding 5).
    rc1 = resolve_mod.cmd_doctor() or 0
    rc2 = _reviewgate_doctor()
    return rc1 or rc2


def add_doctor_parser(subparsers):
    p = subparsers.add_parser("doctor",
                              help="Show detected backends + gate status; write nothing")
    p.set_defaults(func=lambda a: sys.exit(_doctor_and_gate()))


def add_setup_parser(subparsers):
    p = subparsers.add_parser(
        "setup",
        help="Install slash commands/hooks/rules into ~/.claude + the reviewgate "
             "pre-push hook, then resolve models")
    p.add_argument("--claude-home", type=str, default=None,
                   help="Target config dir (default: $CLAUDE_HOME or ~/.claude)")
    p.add_argument("--auto", action="store_true", help="Resolve with everything detected")
    p.add_argument("--claude-only", action="store_true", help="Resolve to the Claude-only floor")
    p.add_argument("--hooks", choices=["compose", "repo-local"], default=None,
                   help="reviewgate hooksPath regime when core.hooksPath is non-default")
    p.set_defaults(func=cmd_setup)


def cmd_setup(args):
    """Install the ~/.claude assets from the packaged base/, then resolve models.

    This is the tool-install counterpart to setup.sh (which symlinks from a
    clone). It COPIES base/{rules,commands,hooks} file-by-file, so it merges
    into an existing ~/.claude instead of replacing whole directories.
    """
    import os
    import shutil
    from . import paths

    dest = Path(args.claude_home or os.environ.get("CLAUDE_HOME")
                or os.path.join(os.path.expanduser("~"), ".claude"))
    base = paths.base_dir()
    if not base.is_dir():
        print(f"error: packaged assets not found at {base}", file=sys.stderr)
        sys.exit(1)

    dest.mkdir(parents=True, exist_ok=True)
    for sub in ("rules", "commands", "hooks"):
        src = base / sub
        if not src.is_dir():
            continue
        (dest / sub).mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copy2(f, dest / sub / f.name)
        print(f"  installed {sub}/ → {dest / sub}")

    # Register the secret-scan hook in ~/.claude/settings.json so it actually
    # runs. Merge-semantic: preserves other tools' hooks, idempotent by
    # script-path identity (running setup twice is a no-op on the hook list).
    from . import hook_setup
    hook_script = dest / "hooks" / "pre-tool-secret-scan.py"
    try:
        result = hook_setup.register(dest, hook_script)
        print(f"  hook {result['status']}: {result['settings_path']}")
    except RuntimeError as e:
        print(f"  hook registration skipped: {e}", file=sys.stderr)

    # Install the reviewgate pre-push hook from potluck's own vendored copy, if
    # run inside a git repo (the hook is repo-local, and self-contained on a
    # stock clone). Skipped with a note outside a work tree.
    import subprocess as _sp
    inside = _sp.run(["git", "rev-parse", "--is-inside-work-tree"],
                     stdout=_sp.DEVNULL, stderr=_sp.DEVNULL).returncode == 0
    if inside:
        vendored = Path(__file__).parent / "_vendor" / "reviewgate.py"
        rg = ["python3", str(vendored), "--install"]
        if getattr(args, "hooks", None):
            rg.append("--hooks=" + args.hooks)
        rg_rc = _sp.run(rg).returncode
        if rg_rc != 0:
            # A refusal (e.g. non-default hooksPath) or any install failure must
            # STOP setup — never fall through to a successful resolve and leave
            # the repo ungated (finding 4).
            print("  reviewgate hook install FAILED (exit %d) — stopping; the gate "
                  "is NOT in place. Re-run per the message above." % rg_rc,
                  file=sys.stderr)
            sys.exit(rg_rc)
    else:
        print("  reviewgate hook: skipped (not inside a git work tree)")

    print(f"\nAssets installed into {dest}. Restart Claude Code to pick up new commands.\n")
    rc = resolve_mod.cmd_resolve(auto=args.auto, claude_only=args.claude_only)
    sys.exit(rc)


# ---------------------------------------------------------------------------
# main — dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="potluck",
        description="Multi-model code-writing harness: bring whatever models you have.",
    )
    sub = p.add_subparsers(dest="command")
    add_fix_parser(sub)
    add_deliver_parser(sub)
    add_resolve_parser(sub)
    add_doctor_parser(sub)
    add_setup_parser(sub)

    # Backward compat: top-level --spec still works (maps to fix)
    p.add_argument("--spec", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--spec-file", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--acceptance", type=str, default="All tests pass.", help=argparse.SUPPRESS)
    p.add_argument("--acceptance-file", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--profile", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--no-debate", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-tiebreaker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--max-rounds", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--gate-timeout", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--doer-timeout", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json-output", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--log-file", type=str, default=None, help=argparse.SUPPRESS)
    return p


def main():
    # Native-Windows fails ungracefully deep in run_subprocess because its
    # cancellation teardown uses POSIX process-group signalling (`os.killpg`),
    # which is POSIX-only. That's an obscure traceback for someone who just
    # tried potluck on the wrong OS. Fail honestly at
    # startup instead: potluck's docs say Windows via WSL, so tell the user
    # exactly that. Under WSL, sys.platform is `linux`, so this check
    # doesn't fire.
    if sys.platform.startswith("win"):
        print(
            "potluck: Windows is not supported natively (uses POSIX "
            "process-group signalling). Please run under WSL2 (Ubuntu or "
            "similar). See potluck's README for setup.",
            file=sys.stderr,
        )
        sys.exit(2)

    parser = build_parser()
    args = parser.parse_args()

    if args.command:
        args.func(args)
    elif args.spec or args.spec_file:
        cmd_fix(args)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  potluck resolve --auto")
        print("  potluck fix --spec 'Fix the pagination off-by-one'")
        sys.exit(1)


if __name__ == "__main__":
    main()
