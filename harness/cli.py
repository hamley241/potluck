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
import json
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
                       checkpointer=None) -> Orchestrator:
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
    default) keeps the exact non-resumable behaviour."""
    doer = RealDoerClient()
    reviewer = ReviewerClient(cfg)
    tiebreaker = TiebreakerClient(cfg) if cfg.debate.use_tiebreaker else None
    restore_tree = None if allow_dirty else real_restore_tree
    orch = Orchestrator(cfg, doer, reviewer, tiebreaker, real_run_gate,
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


def _refuse_run_dir_inside_tree(run_dir: str) -> None:
    """Refuse a run-dir inside the working tree (fail-closed). §2.3 restore does
    `git clean -fdx`, which would DELETE a run-dir anywhere under the repo (even a
    gitignored one), and the tree snapshot would CAPTURE it -- both corrupt the
    checkpoint. The sidecar must live outside the tree it snapshots.

    Containment is checked against the git WORKING-TREE ROOT (`git rev-parse
    --show-toplevel`), NOT cwd (codex round-6 MAJOR): invoking from a subdirectory
    with `--run-dir ../runs/x` can resolve OUTSIDE cwd yet INSIDE the repo, which a
    cwd-based check would wrongly accept and clean -fdx would still destroy."""
    import subprocess
    rd = Path(run_dir).resolve()
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # Not in a git repo (or git unreadable): fall back to cwd -- there is no
        # tree to snapshot/clean, so the only hazard is a run-dir under cwd.
        root = Path.cwd().resolve()
    else:
        root = Path(proc.stdout.strip()).resolve()
    if rd == root or root in rd.parents:
        print(f"Error: --run-dir/--resume path ({rd}) is INSIDE the git working "
              f"tree ({root}). The checkpoint sidecar must live OUTSIDE the tree "
              f"it snapshots -- restore's `git clean -fdx` would delete it and "
              f"the snapshot would capture it. Use a path outside the repo, e.g. "
              f"../potluck-runs/<id>.", file=sys.stderr)
        sys.exit(1)


def _build_checkpointer(run_dir: str, cfg: HarnessConfig, spec: str,
                        acceptance: str):
    """Construct a git-backed Checkpointer for `run_dir` bound to this run's
    identity inputs (hardening Step-0 §2)."""
    from . import checkpoint as ck
    _refuse_run_dir_inside_tree(run_dir)
    cfg_digest = ck.config_digest(_effective_config_dict(cfg))
    base = _git_head()
    manifest_id = ck.compute_manifest_id(spec, acceptance, cfg_digest, base)
    store = ck.GitCheckpointStore(Path(run_dir), repo=Path.cwd())
    return ck.Checkpointer(store, spec, acceptance, cfg_digest, manifest_id), \
        cfg_digest, base, manifest_id


def ensure_clean_tree(allow_dirty: bool = False) -> str | None:
    """Clean-tree precondition for `potluck fix`. Returns an actionable error
    message when the working tree is dirty (`git status --porcelain` non-empty)
    and --allow-dirty was NOT passed, else None. Extracted from cmd_fix so the
    precondition is testable without driving the whole CLI.

    A dead implement attempt leaves partial edits in the tree; starting from a
    known-clean baseline is what lets restore_tree reset to it safely.

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
                        "precondition AND all tree-restore behavior")
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
    if args.gate_timeout is not None or args.doer_timeout is not None:
        try:
            cfg.validate()
        except ValueError as e:
            print(f"Error: invalid timeout override: {e}", file=sys.stderr)
            sys.exit(1)


def _require_gate() -> None:
    if not Path(".claude/verify.sh").exists():
        print("Error: .claude/verify.sh not found.\n"
              "Copy verify.sh.example into your project as .claude/verify.sh.",
              file=sys.stderr)
        sys.exit(1)


def cmd_fix(args):
    from . import checkpoint as ck
    resume_dir = getattr(args, "resume", None)
    run_dir = getattr(args, "run_dir", None)
    allow_dirty = getattr(args, "allow_dirty", False)

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
        _require_gate()
        # The tree must be clean at the checkpoint base for a lossless restore.
        dirty_err = ensure_clean_tree(allow_dirty)
        if dirty_err:
            print(f"Error: {dirty_err}", file=sys.stderr)
            sys.exit(1)
        checkpointer, *_ = _build_checkpointer(resume_dir, cfg, spec, acceptance)
        try:
            resume_ckpt = checkpointer.load_and_verify()
            checkpointer.restore(resume_ckpt.tree)
        except ck.CheckpointError as e:
            print(f"Error: resume refused (fail-closed): {e}", file=sys.stderr)
            sys.exit(1)
        print(f"potluck: RESUMING from {resume_dir} at "
              f"{resume_ckpt.cursor.phase.value} (round {resume_ckpt.cursor.round_index})")
        print(f"spec: {spec[:80]}{'...' if len(spec) > 80 else ''}\n")

        async def _run():
            orch = build_orchestrator(cfg, allow_dirty=allow_dirty,
                                      checkpointer=checkpointer)
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
        _require_gate()
        # Clean-tree precondition (before the banner): a dead implement attempt
        # leaves partial edits behind, so we start from a known-clean baseline
        # restore_tree can reset to. --allow-dirty is the visible opt-out.
        dirty_err = ensure_clean_tree(allow_dirty)
        if dirty_err:
            print(f"Error: {dirty_err}", file=sys.stderr)
            sys.exit(1)

        checkpointer = None
        if run_dir:
            checkpointer, cfg_digest, base, manifest_id = _build_checkpointer(
                run_dir, cfg, spec, acceptance)
            ck.persist_run_inputs(run_dir, spec, acceptance, cfg_digest, base,
                                  manifest_id)

        print(f"potluck: profile={cfg.profile}, debate={'on' if cfg.debate_enabled else 'off'}, "
              f"max_rounds={cfg.debate.max_rounds}"
              + (f", checkpoint={run_dir}" if run_dir else ""))
        print(f"roles: reviewer={cfg.models.reviewer.name}, tiebreaker={cfg.models.tiebreaker.name}")
        print(f"spec: {spec[:80]}{'...' if len(spec) > 80 else ''}\n")

        async def _run():
            orch = build_orchestrator(cfg, allow_dirty=allow_dirty,
                                      checkpointer=checkpointer)
            return await orch.run_feature(spec, acceptance)

        result = asyncio.run(_run())
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
    sys.exit(0 if out["outcome"] == "passed" else 1)


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
