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
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# Known install locations to probe when a binary isn't on PATH. Codex ships
# inside the desktop app bundle; Kimi installs under the home dir.
CODEX_PATHS = [
    "/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
]
KIMI_PATHS = [os.path.expanduser("~/.kimi-code/bin/kimi")]

# Claude models for the two resolved roles when they fall back to Claude. The
# doer runs on whatever the interactive session defaults to (usually Opus), so
# these two + the doer give three distinct Claude models across the roles.
CLAUDE_REVIEWER_MODEL = "sonnet"
CLAUDE_TIEBREAKER_MODEL = "haiku"


def _toml_str(s: str) -> str:
    """Render a string as a TOML basic string, escaping backslashes and quotes
    so a path with such characters can't produce an invalid .resolved.toml."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class Backend:
    name: str
    cmd: list[str]
    fmt: str
    stdin: bool = True   # True: prompt on stdin; False: prompt appended as argv

    def as_toml_table(self) -> str:
        cmd_items = ", ".join(_toml_str(c) for c in self.cmd)
        return (f'name = {_toml_str(self.name)}\n'
                f'cmd = [{cmd_items}]\n'
                f'fmt = {_toml_str(self.fmt)}\n'
                f'stdin = {"true" if self.stdin else "false"}\n')


# --- backend builders ---

def claude_backend(model: str, claude_path: str = "claude") -> Backend:
    # `claude -p` reads the prompt from stdin. `--tools ""` disables ALL tools so
    # the reviewer/tiebreaker are genuinely read-only judgment calls -- they
    # cannot edit the workspace or run commands, matching Codex's read-only
    # sandbox. (The doer is a separate call that keeps its edit tools.)
    return Backend("claude", [claude_path, "-p", "--tools", "", "--model", model],
                   "text", stdin=True)


def codex_backend(path: str) -> Backend:
    # `codex exec` reads the prompt from stdin. Read-only sandbox: the reviewer
    # grades code, it never edits it.
    return Backend("codex", [path, "exec", "--json", "--sandbox", "read-only"],
                   "codex_jsonl", stdin=True)


def kimi_backend(path: str) -> Backend:
    # `kimi -p <prompt>` takes the prompt as an ARGUMENT, not on stdin, so the
    # runner appends it to cmd.
    # Caveat: the tiebreak prompt embeds the diff, so on a very large change it
    # can exceed the OS per-arg / ARG_MAX limit. That surfaces as an OSError,
    # which the StepRunner catches and the loop escalates as ESCALATED_NO_SIGNAL
    # (a missing adjudication, never a silent "approved"). It is not a crash,
    # but Kimi tiebreak is unavailable on outsized diffs -- a known v1 limit.
    return Backend("kimi", [path, "-p"], "text", stdin=False)


# --- detection ---

def _find(name: str, extra_paths: list[str]) -> str | None:
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for candidate in extra_paths:
        # Must be an executable FILE -- a directory or non-executable file at a
        # known path is not a usable backend, so fall through to the Claude
        # fallback rather than write a dead command into .resolved.toml.
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def detect() -> dict[str, str | None]:
    """Return {model_name: resolved_path_or_None} for each supported backend."""
    return {
        "claude": _find("claude", []),
        "codex": _find("codex", CODEX_PATHS),
        "kimi": _find("kimi", KIMI_PATHS),
    }


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


def _model_suffix(b: Backend) -> str:
    if b.name == "claude" and "--model" in b.cmd:
        return f" ({b.cmd[b.cmd.index('--model') + 1]})"
    return ""


# --- writing the resolved plan ---

def render_resolved_toml(roles: dict[str, Backend]) -> str:
    return (
        "# Machine-local model resolution -- written by `potluck resolve`.\n"
        "# Gitignored on purpose: paths differ per machine, like credentials.\n"
        "# Re-run `potluck resolve` on each machine; do not commit this file.\n\n"
        "[models.reviewer]\n"
        f"{roles['reviewer'].as_toml_table()}\n"
        "[models.tiebreaker]\n"
        f"{roles['tiebreaker'].as_toml_table()}"
    )


def resolved_path() -> Path:
    return Path(__file__).parent.parent / ".resolved.toml"


def write_resolved(roles: dict[str, Backend], path: Path | None = None) -> Path:
    path = path or resolved_path()
    path.write_text(render_resolved_toml(roles))
    return path


# --- interactive prompts ---

def _ask_use(model: str, path: str | None) -> bool:
    if not path:
        print(f"  {model}: not found on this machine -- skipping "
              f"(the {model} role falls back to a Claude model).")
        return False
    ans = input(f"  Use {model} for its role? found at {path}  [Y/n] ").strip().lower()
    return ans in ("", "y", "yes")


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
