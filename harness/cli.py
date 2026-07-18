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
                       allow_dirty: bool = False) -> Orchestrator:
    """Wire a cfg into a production Orchestrator (real doer/reviewer/tiebreaker,
    real gate, config-bound get_diff, real tree restore). Pure: cfg ->
    Orchestrator, no argparse and no I/O, so a test can build the exact object
    cmd_fix runs and assert on the wiring (e.g. `orch.get_diff.diff_cfg is
    cfg.diff`).

    restore_tree is the real `git reset --hard` + `git clean -fd` when
    allow_dirty is False; when True (operator opted out of the clean-tree
    precondition), it is a no-op -- we cannot safely reset a tree whose
    baseline we don't own -- and we log a `tree_hygiene_disabled` event so the
    transcript shows hygiene was turned off."""
    doer = RealDoerClient()
    reviewer = ReviewerClient(cfg)
    tiebreaker = TiebreakerClient(cfg) if cfg.debate.use_tiebreaker else None
    restore_tree = None if allow_dirty else real_restore_tree
    orch = Orchestrator(cfg, doer, reviewer, tiebreaker, real_run_gate,
                        bound_get_diff(cfg.diff), restore_tree=restore_tree)
    if allow_dirty:
        orch._log("tree_hygiene_disabled", allow_dirty=True)
    return orch


def ensure_clean_tree(allow_dirty: bool = False) -> str | None:
    """Clean-tree precondition for `potluck fix`. Returns an actionable error
    message when the working tree is dirty (`git status --porcelain` non-empty)
    and --allow-dirty was NOT passed, else None. Extracted from cmd_fix so the
    precondition is testable without driving the whole CLI.

    A dead implement attempt leaves partial edits in the tree; starting from a
    known-clean baseline is what lets restore_tree reset to it safely.

    Safe-default rule: if we cannot PROVE the tree is clean, we refuse. Empty
    porcelain output is only meaningful when the command actually SUCCEEDED --
    outside a git repo, with git missing from PATH, or on a permissions error,
    stdout is empty too and `porcelain.strip()` is falsy, which would silently
    read as "clean" (worse than a false failure: a failed check that passes).
    So we check the OUTCOME, not just stdout -- the same lesson the gate learned
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
    except FileNotFoundError:
        return ("could not verify the working tree: git is not on PATH. "
                "Install git, or pass --allow-dirty to skip the check.")
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
    p.add_argument("--allow-dirty", action="store_true",
                   help="Run against a dirty working tree; skips the clean-tree "
                        "precondition AND all tree-restore behavior")
    p.add_argument("--json-output", action="store_true")
    p.add_argument("--log-file", type=str, default=None)
    p.set_defaults(func=cmd_fix)


def cmd_fix(args):
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
    if args.gate_timeout:
        cfg.timeouts.gate_seconds = args.gate_timeout

    gate = Path(".claude/verify.sh")
    if not gate.exists():
        print("Error: .claude/verify.sh not found.\n"
              "Copy verify.sh.example into your project as .claude/verify.sh.",
              file=sys.stderr)
        sys.exit(1)

    # Clean-tree precondition (before the banner): a dead implement attempt
    # leaves partial edits behind, so we start from a known-clean baseline that
    # restore_tree can reset to. --allow-dirty is the visible, committed opt-out.
    allow_dirty = getattr(args, "allow_dirty", False)
    dirty_err = ensure_clean_tree(allow_dirty)
    if dirty_err:
        print(f"Error: {dirty_err}", file=sys.stderr)
        sys.exit(1)

    print(f"potluck: profile={cfg.profile}, debate={'on' if cfg.debate_enabled else 'off'}, "
          f"max_rounds={cfg.debate.max_rounds}")
    print(f"roles: reviewer={cfg.models.reviewer.name}, tiebreaker={cfg.models.tiebreaker.name}")
    print(f"spec: {spec[:80]}{'...' if len(spec) > 80 else ''}\n")

    async def _run():
        orch = build_orchestrator(cfg, allow_dirty=allow_dirty)
        return await orch.run_feature(spec, acceptance)

    result = asyncio.run(_run())
    out = {
        "outcome": result.outcome.value,
        "rounds_used": result.rounds_used,
        "escalation_reason": result.escalation_reason,
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


def add_doctor_parser(subparsers):
    p = subparsers.add_parser("doctor", help="Show detected backends; write nothing")
    p.set_defaults(func=lambda a: sys.exit(resolve_mod.cmd_doctor()))


def add_setup_parser(subparsers):
    p = subparsers.add_parser(
        "setup",
        help="Install slash commands/hooks/rules into ~/.claude, then resolve models")
    p.add_argument("--claude-home", type=str, default=None,
                   help="Target config dir (default: $CLAUDE_HOME or ~/.claude)")
    p.add_argument("--auto", action="store_true", help="Resolve with everything detected")
    p.add_argument("--claude-only", action="store_true", help="Resolve to the Claude-only floor")
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
