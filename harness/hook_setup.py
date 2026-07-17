"""Register potluck's PreToolUse hook in Claude Code's settings.json.

Called from both `potluck setup` (Python) and setup.sh (via `python -m
harness.hook_setup`) so the two install paths stay feature-matched.

Merge semantics matter: we're touching a shared config file that other tools
may write to. The rules:

- Read existing settings.json (or start with `{}` if absent). Preserve every
  key we don't own.
- Add our hook entry only if no existing entry references the same script
  path (semantic-identity duplicate suppression -- running setup twice, or
  after the user already added the hook by hand, must be a no-op).
- Stable ordering: entries we write always appear in a deterministic
  position within the hooks list so subsequent diffs are minimal.

Without these, `potluck setup` would silently clobber unrelated hooks -- and
the README's promise that setup "won't replace your existing config" would
be false at the hook level.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Which Claude Code tools should trigger the hook. These are the tools that
# can plausibly send project content OUT of the local environment (edits are
# followed by a diff-and-review that goes to an external reviewer CLI).
_HOOK_MATCHER = "Bash|Edit|Write"


def _hook_entry(hook_script: Path) -> dict:
    """Shape of a single PreToolUse hook entry, per Claude Code's
    settings.json schema. `command` is invoked with the tool payload on
    stdin; a non-zero exit blocks the tool call."""
    return {
        "matcher": _HOOK_MATCHER,
        "hooks": [
            {"type": "command", "command": str(hook_script)},
        ],
    }


def _hook_command(entry: dict) -> str | None:
    """Extract the script path from a PreToolUse entry so we can compare by
    semantic identity (same script = same hook) rather than dict equality."""
    hooks = entry.get("hooks") or []
    if hooks and isinstance(hooks[0], dict):
        return hooks[0].get("command")
    return None


def register(claude_home: Path, hook_script: Path) -> dict:
    """Ensure `hook_script` is wired as a PreToolUse hook in
    `claude_home/settings.json`. Idempotent by script-path identity. Returns
    a small dict describing what happened, mostly for tests / setup output."""
    settings_path = claude_home / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            # Malformed existing settings -- refuse to overwrite blindly.
            raise RuntimeError(
                f"{settings_path} is not valid JSON; refusing to edit "
                f"until you fix it by hand."
            )
    else:
        settings = {}
    if not isinstance(settings, dict):
        raise RuntimeError(
            f"{settings_path} top level is not an object; refusing to edit."
        )

    hooks_root = settings.setdefault("hooks", {})
    pretool = hooks_root.setdefault("PreToolUse", [])
    if not isinstance(pretool, list):
        raise RuntimeError(
            f"{settings_path} hooks.PreToolUse is not a list; "
            f"refusing to edit."
        )

    script_str = str(hook_script)
    for existing in pretool:
        if isinstance(existing, dict) and _hook_command(existing) == script_str:
            # Already wired -- idempotent no-op.
            return {"status": "already_present", "settings_path": str(settings_path)}

    pretool.append(_hook_entry(hook_script))
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return {"status": "added", "settings_path": str(settings_path)}


def main() -> int:
    """CLI entry: `python -m harness.hook_setup [CLAUDE_HOME] [HOOK_SCRIPT]`.
    Both args are optional; defaults match the same env resolution
    `potluck setup` uses."""
    import sys
    claude_home = Path(
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("CLAUDE_HOME")
             or os.path.join(os.path.expanduser("~"), ".claude")
    )
    default_script = claude_home / "hooks" / "pre-tool-secret-scan.py"
    hook_script = Path(sys.argv[2]) if len(sys.argv) > 2 else default_script
    result = register(claude_home, hook_script)
    print(f"  hook {result['status']}: {result['settings_path']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
