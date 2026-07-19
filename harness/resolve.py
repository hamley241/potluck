"""Model resolution: detect what's installed, ask what to use, write the plan.

potluck's premise is a potluck -- you bring whatever models you have on hand.
This module decides which backend fills each role:

  doer       always Claude Code (this harness runs around it)
  reviewer   Codex if you have it and want it, else a Claude model
  tiebreaker Kimi  if you have it and want it, else a Claude model

Claude-only is the floor that always works (one `claude` CLI, three distinct
models across the roles). Adding Codex/Kimi is a real upgrade in *independence*
-- foreign models fail differently than Claude, so they catch Claude's blind
spots -- not just redundancy. That is the whole point of a second opinion.

The resolved plan is written to `.resolved.toml` (gitignored, machine-local),
which HarnessConfig.load layers on top of the committed profile. Model paths
differ per machine and never belong in git -- same rule as credentials.

Usage:
    potluck resolve            # interactive: ask per external model
    potluck resolve --auto     # use Codex/Kimi if detected, no prompts
    potluck resolve --claude-only
    potluck doctor             # print detection, don't write anything
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# PORTED to harness_core 2026-07-18 (engine extraction, step 2).
# The role-free half — backend construction, detection, single-
# backend health check — now lives in the core. What remains here
# names potluck's ROLES (reviewer, tiebreaker), which is exactly
# what HC4 keeps out of a shared core. harness/resolve.py's local
# copies of the moved functions are gone from THIS file, but the
# module itself stays until step 4 per the HC1 ruling.
from harness_core.resolve import (  # noqa: F401  (re-exported)
    CODEX_PATHS, KIMI_PATHS, Backend, _ask_use, _find,
    _model_suffix, _toml_str, check_backend, claude_backend,
    codex_backend, detect, kimi_backend)

# Known install locations to probe when a binary isn't on PATH. Codex ships
# inside the desktop app bundle; Kimi installs under the home dir.

# Claude models for the two resolved roles when they fall back to Claude. The
# doer runs on whatever the interactive session defaults to (usually Opus), so
# these two + the doer give three distinct Claude models across the roles.
CLAUDE_REVIEWER_MODEL = "sonnet"
CLAUDE_TIEBREAKER_MODEL = "haiku"






# --- backend builders ---







# --- detection ---





# --- role resolution (pure; this is what the tests exercise) ---

def resolve_roles(detected: dict[str, str | None],
                  use_codex: bool, use_kimi: bool) -> dict[str, Backend]:
    """Map reviewer + tiebreaker to concrete backends.

    A model is only slotted in if it is BOTH wanted (use_*) AND actually
    detected. Anything else falls back to a distinct Claude model, so the loop
    always has a reviewer and a tiebreaker.
    """
    claude_path = detected.get("claude") or "claude"

    if use_codex and detected.get("codex"):
        reviewer = codex_backend(detected["codex"])
    else:
        reviewer = claude_backend(CLAUDE_REVIEWER_MODEL, claude_path)

    if use_kimi and detected.get("kimi"):
        tiebreaker = kimi_backend(detected["kimi"])
    else:
        tiebreaker = claude_backend(CLAUDE_TIEBREAKER_MODEL, claude_path)

    return {"reviewer": reviewer, "tiebreaker": tiebreaker}


def plan_summary(roles: dict[str, Backend]) -> str:
    r, t = roles["reviewer"], roles["tiebreaker"]
    indep = (r.name != "claude") or (t.name != "claude")
    line = (f"  doer       claude (this session)\n"
            f"  reviewer   {r.name}{_model_suffix(r)}\n"
            f"  tiebreaker {t.name}{_model_suffix(t)}")
    note = ("  independence: cross-provider (foreign models catch Claude's blind spots)"
            if indep else
            "  independence: Claude-only floor (works everywhere; weaker second opinion)")
    return f"{line}\n{note}"




# --- health checks ---



def check_models(models) -> list[str]:
    """Return problem strings for unhealthy backends; empty list means all healthy."""
    problems = []
    for b in (models.reviewer, models.tiebreaker):
        msg = check_backend(b)
        if msg is not None:
            problems.append(msg)
    return problems


# --- writing the resolved plan ---

def render_resolved_toml(roles: dict[str, Backend]) -> str:
    return (
        "# Machine-local model resolution -- written by `potluck resolve`.\n"
        "# Lives in your user config dir (~/.config/potluck), not the source tree:\n"
        "# paths differ per machine, like credentials. Re-run `potluck resolve`\n"
        "# on each machine; never commit this file.\n\n"
        "[models.reviewer]\n"
        f"{roles['reviewer'].as_toml_table()}\n"
        "[models.tiebreaker]\n"
        f"{roles['tiebreaker'].as_toml_table()}"
    )


def resolved_path() -> Path:
    from .paths import resolved_path as _rp
    return _rp()


def write_resolved(roles: dict[str, Backend], path: Path | None = None) -> Path:
    path = path or resolved_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_resolved_toml(roles))
    return path


# --- interactive prompts ---



def _ask_install_mode(model: str) -> None:
    # v1 wires the LOCAL CLI path (detected above). API-key backends are a
    # documented seam, not yet wired -- we surface the choice honestly rather
    # than pretend it works.
    ans = input(f"  {model}: use the local CLI, or an API key?  [cli/api] ").strip().lower()
    if ans.startswith("a"):
        print(f"    note: direct API-key calling for {model} is not wired in v1; "
              f"using the local CLI. (See resolve.py: API backend seam.)")


def interactive_resolve(detected: dict[str, str | None]) -> dict[str, Backend]:
    print("potluck resolve -- which models should fill each role?\n")
    print(f"  claude: {'found at ' + detected['claude'] if detected['claude'] else 'NOT FOUND (required)'}")
    if not detected["claude"]:
        print("\n  Claude is the required doer/floor. Install the `claude` CLI first.")
        sys.exit(1)

    use_codex = _ask_use("codex", detected["codex"])
    if use_codex:
        _ask_install_mode("codex")
    use_kimi = _ask_use("kimi", detected["kimi"])
    if use_kimi:
        _ask_install_mode("kimi")

    return resolve_roles(detected, use_codex, use_kimi)


def auto_resolve(detected: dict[str, str | None], claude_only: bool = False) -> dict[str, Backend]:
    if claude_only:
        return resolve_roles(detected, use_codex=False, use_kimi=False)
    return resolve_roles(detected, use_codex=True, use_kimi=True)


# --- CLI entry (wired into harness/cli.py as `resolve` and `doctor`) ---

def cmd_doctor() -> int:
    detected = detect()
    print("potluck doctor -- detected backends:\n")
    for name in ("claude", "codex", "kimi"):
        p = detected[name]
        print(f"  {name:8} {p if p else '(not found)'}")
    print()
    print("Would resolve (auto, use everything detected) to:")
    print(plan_summary(auto_resolve(detected)))
    rp = resolved_path()
    print(f"\nActive resolution file: {rp if rp.exists() else '(none -- Claude-only defaults)'}")
    return 0


def cmd_resolve(auto: bool = False, claude_only: bool = False) -> int:
    detected = detect()
    # Claude is the doer AND the fallback floor for every role, so it is
    # required in ALL modes -- not just interactive. Fail loudly rather than
    # write a resolution that points at a nonexistent `claude` command.
    if not detected["claude"]:
        print("error: the required `claude` CLI was not found on PATH.\n"
              "Claude is the doer and the fallback for every role. Install it, "
              "then re-run `potluck resolve`.", file=sys.stderr)
        return 1
    if claude_only:
        roles = auto_resolve(detected, claude_only=True)
    elif auto:
        roles = auto_resolve(detected)
    else:
        roles = interactive_resolve(detected)
    path = write_resolved(roles)
    print("\nResolved plan:")
    print(plan_summary(roles))
    print(f"\nWrote {path}")
    return 0
