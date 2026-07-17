#!/usr/bin/env bash
# Symlink base + chosen profile into ~/.claude, then resolve this machine's
# models. One clone, one command, per device.
#
#   ./setup.sh personal      # personal machines
#   ./setup.sh phi           # HIPAA/PHI codebase machines
#   ./setup.sh ci            # CI runners / containers
#
# Credentials are NEVER touched -- re-authenticate each model CLI per machine.
set -euo pipefail

PROFILE="${1:-}"
if [[ -z "$PROFILE" || ! -d "profiles/$PROFILE" ]]; then
  echo "usage: ./setup.sh <personal|phi|ci>" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"
mkdir -p "$DEST"

link() { ln -sfn "$1" "$2"; echo "  linked $(basename "$2")"; }

echo "Installing profile '$PROFILE' into $DEST"
# Base layer (shared everywhere)
link "$REPO/base/rules"     "$DEST/rules"
link "$REPO/base/commands"  "$DEST/commands"
link "$REPO/base/hooks"     "$DEST/hooks"
[[ -f "$REPO/base/CLAUDE.md" ]] && link "$REPO/base/CLAUDE.md" "$DEST/CLAUDE.md"
# Chosen profile config
link "$REPO/profiles/$PROFILE/profile.toml" "$DEST/profile.toml"

# potluck CLI wrapper — symlink into ~/.local/bin so it's on PATH
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
link "$REPO/potluck" "$BIN_DIR/potluck"

# Register the secret-scan hook in Claude Code's settings.json. Merge-safe
# and idempotent -- won't clobber existing hooks other tools installed.
# Same helper the `potluck setup` (install path) uses, so both paths stay
# feature-matched. Pure stdlib -- no uv/pydantic needed.
CLAUDE_HOME="$DEST" PYTHONPATH="$REPO" python3 -m harness.hook_setup \
  || echo "  (hook registration skipped -- run 'potluck setup' manually)"

# Resolve which models fill each role on THIS machine. CI is non-interactive,
# so auto-resolve there; personal/phi ask per external model.
echo
if [[ "$PROFILE" == "ci" ]]; then
  echo "Resolving models (auto -- CI is non-interactive)..."
  "$REPO/potluck" resolve --auto || echo "  (resolve skipped; Claude-only defaults apply)"
else
  echo "Resolving models for this machine..."
  "$REPO/potluck" resolve || echo "  (resolve skipped; Claude-only defaults apply -- run 'potluck resolve' later)"
fi

echo
echo "Done. Active profile: $PROFILE"
echo
echo "To use potluck:"
echo "  From a terminal:     cd your-project && potluck fix --spec 'Fix the bug'"
echo "  From Claude Code:    /potluck Fix the pagination bug"
echo "  Inspect model wiring: potluck doctor"
echo
echo "Reminder: authenticate each external model CLI on this machine (codex login, etc.)."
echo "Note: after pulling command changes, restart the Claude Code session"
echo "      -- changed commands can be served from cache otherwise (known issue)."
