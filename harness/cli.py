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
import json
import os
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
    if hasattr(args, "log_file") and args.log_file:
        Path(args.log_file).write_text(json.dumps(result, indent=2, default=str))
        print(f"\nFull log written to {args.log_file}")


def build_orchestrator(cfg: HarnessConfig,
                       allow_dirty: bool = False,
                       checkpointer=None,
                       gate_path: str | None = None) -> Orchestrator:
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
    bound here (partial) rather than passed at the call site."""
    doer = RealDoerClient()
    reviewer = ReviewerClient(cfg)
    tiebreaker = TiebreakerClient(cfg) if cfg.debate.use_tiebreaker else None
    restore_tree = None if allow_dirty else real_restore_tree
    run_gate = (real_run_gate if gate_path is None
                else functools.partial(real_run_gate, gate_path))
    orch = Orchestrator(cfg, doer, reviewer, tiebreaker, run_gate,
                        bound_get_diff(cfg.diff), restore_tree=restore_tree,
                        checkpointer=checkpointer)
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


def _build_checkpointer(run_dir: str, cfg: HarnessConfig, spec: str,
                        acceptance: str, gate_binding_digest: str):
    """Construct a git-backed Checkpointer for `run_dir` bound to this run's
    identity inputs (hardening Step-0 §2), including the gate binding (conductor §5)."""
    from . import checkpoint as ck
    _refuse_run_dir_inside_tree(run_dir)
    cfg_digest = ck.config_digest(_effective_config_dict(cfg))
    base = _git_head()
    manifest_id = ck.compute_manifest_id(spec, acceptance, cfg_digest, base)
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

    def deliver(self) -> str | None:
        """Apply the run's change (base -> the worktree's final tree, untracked
        non-ignored files included) to the operator's working tree. Returns None
        on success, else an error message; writes nothing to the operator's tree
        on any failure."""
        import subprocess
        import tempfile
        wt = str(self.path)

        def git(*args, cwd=wt, env=None, data=None):
            return subprocess.run(["git", *args], cwd=cwd, input=data,
                                  env={**os.environ, **env} if env else None,
                                  capture_output=True)

        head = git("rev-parse", "HEAD").stdout.decode().strip()
        if head != self.base:
            return ("the doer COMMITTED in the run's worktree (HEAD moved off the "
                    "base), so part of its work never reached the review diff; "
                    "nothing was applied to your tree")
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
            return f"could not compute the run's change: {failed.stderr.decode()[:300]}"
        if not diff.stdout:
            return None
        applied = git("apply", cwd=str(self.root), data=diff.stdout)
        if applied.returncode == 0:
            return None
        fd, saved = tempfile.mkstemp(prefix="potluck-unapplied-", suffix=".patch")
        with os.fdopen(fd, "wb") as f:
            f.write(diff.stdout)
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
        proc = subprocess.run(
            ["git", "worktree", "remove", "--force", "--force", str(self.path)],
            cwd=str(self.root), capture_output=True)
        if proc.returncode != 0:
            shutil.rmtree(self.path, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(self.root),
                           capture_output=True)
        # REPORT a cleanup that did not work (codex BLOCKING, r4). Both fallbacks
        # above swallow their own failure -- rmtree(ignore_errors=True) by
        # definition -- so without this check a run could leave its worktree
        # behind and say nothing, which is the silence A5 exists to prevent.
        if Path(self.path).exists():
            print(f"Warning: the run's worktree could not be removed and is "
                  f"still at {self.path}. It will be swept by the next run in "
                  f"this checkout; remove it by hand if you prefer.",
                  file=sys.stderr)
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


def cmd_fix(args):
    """Run `_cmd_fix` and remove the run's worktree afterwards on EVERY path --
    pass, escalation, refusal (SystemExit), crash, or Ctrl-C."""
    run = _RunWorktree()
    try:
        _cmd_fix(args, run)
    finally:
        run.remove()


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

    if resume_dir:
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
        run.enter()
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
                                      checkpointer=checkpointer, gate_path=gate_path)
            return await orch.run_feature(spec, acceptance, resume=resume_ckpt)

        result = asyncio.run(_run())
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
        run.enter()

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
            else:
                gate_binding = ck.INTREE_GATE_BINDING
                _art_gate = _digest_gate_file(".claude/verify.sh")
            checkpointer, cfg_digest, base, manifest_id = _build_checkpointer(
                run_dir, cfg, spec, acceptance, gate_binding)
            ck.persist_run_inputs(run_dir, spec, acceptance, cfg_digest, base,
                                  manifest_id)
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
                                      checkpointer=checkpointer, gate_path=gate_path)
            return await orch.run_feature(spec, acceptance)

        result = asyncio.run(_run())

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
        "debate_log": result.debate_log,
    }

    if not (hasattr(args, "json_output") and args.json_output):
        outcome = out["outcome"]
        symbol = "PASSED" if outcome == "passed" else f"ESCALATED ({outcome})"
        print(f"\n{'='*60}")
        print(f"  {symbol}  |  Rounds: {out['rounds_used']}")
        if out["escalation_reason"]:
            print(f"  Reason: {out['escalation_reason']}")
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
        if out["outcome"] == "passed" and cr and cr["candidates"]:
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

    write_log(out, args)
    sys.exit(0 if out["outcome"] == "passed" and not delivery_err else 1)


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
