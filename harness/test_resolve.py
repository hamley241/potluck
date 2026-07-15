"""Exercise model resolution -- pure logic, no real CLIs, no model calls.

The contract: given what's DETECTED on the machine and what the user WANTS,
each role resolves to the right backend, and Claude-only is always a valid
floor. This mirrors the four cases we care about:

  all three available      -> reviewer=codex, tiebreaker=kimi
  Claude + Codex only      -> reviewer=codex, tiebreaker=claude
  Claude + Kimi only       -> reviewer=claude, tiebreaker=kimi
  Claude only              -> reviewer=claude, tiebreaker=claude (distinct models)

Plus: "wanted but not installed" must fall back (never resolve to a missing
binary), and the resolved plan must round-trip through .resolved.toml back into
HarnessConfig.
"""

import tomllib
import tempfile
from pathlib import Path

from harness.resolve import (
    resolve_roles, render_resolved_toml, write_resolved, Backend,
    CLAUDE_REVIEWER_MODEL, CLAUDE_TIEBREAKER_MODEL,
)
from harness.config import HarnessConfig

CODEX = "/Applications/Codex.app/Contents/Resources/codex"
KIMI = "/home/u/.kimi-code/bin/kimi"
CLAUDE = "/usr/local/bin/claude"

ALL = {"claude": CLAUDE, "codex": CODEX, "kimi": KIMI}
NO_CODEX = {"claude": CLAUDE, "codex": None, "kimi": KIMI}
NO_KIMI = {"claude": CLAUDE, "codex": CODEX, "kimi": None}
CLAUDE_ONLY = {"claude": CLAUDE, "codex": None, "kimi": None}


def _model_of(backend):
    """The --model value for a claude backend, else None."""
    if backend.name == "claude" and "--model" in backend.cmd:
        return backend.cmd[backend.cmd.index("--model") + 1]
    return None


def main():
    results = {}

    # 1. All three available, both wanted -> foreign models slotted in.
    #    Codex reads stdin; Kimi takes the prompt as an argv arg (stdin=False).
    r = resolve_roles(ALL, use_codex=True, use_kimi=True)
    results["all_three"] = (
        r["reviewer"].name == "codex"
        and r["reviewer"].fmt == "codex_jsonl"
        and CODEX in r["reviewer"].cmd
        and r["reviewer"].stdin is True
        and r["tiebreaker"].name == "kimi"
        and KIMI in r["tiebreaker"].cmd
        and r["tiebreaker"].stdin is False
    )

    # 2. Claude + Codex (no Kimi) -> reviewer=codex, tiebreaker falls back to Claude.
    r = resolve_roles(NO_KIMI, use_codex=True, use_kimi=True)
    results["claude_plus_codex"] = (
        r["reviewer"].name == "codex"
        and r["tiebreaker"].name == "claude"
        and _model_of(r["tiebreaker"]) == CLAUDE_TIEBREAKER_MODEL
    )

    # 3. Claude + Kimi (no Codex) -> reviewer falls back to Claude, tiebreaker=kimi.
    r = resolve_roles(NO_CODEX, use_codex=True, use_kimi=True)
    results["claude_plus_kimi"] = (
        r["reviewer"].name == "claude"
        and _model_of(r["reviewer"]) == CLAUDE_REVIEWER_MODEL
        and r["tiebreaker"].name == "kimi"
    )

    # 4. Claude only -> both roles Claude, on DISTINCT models.
    r = resolve_roles(CLAUDE_ONLY, use_codex=True, use_kimi=True)
    results["claude_only"] = (
        r["reviewer"].name == "claude"
        and r["tiebreaker"].name == "claude"
        and _model_of(r["reviewer"]) != _model_of(r["tiebreaker"])
        and CLAUDE in r["reviewer"].cmd
    )

    # 5. Wanted but NOT installed must fall back -- never point at a missing binary.
    r = resolve_roles(CLAUDE_ONLY, use_codex=True, use_kimi=True)
    results["missing_never_slotted"] = (
        CODEX not in r["reviewer"].cmd and KIMI not in r["tiebreaker"].cmd
    )

    # 6. Installed but NOT wanted must fall back to Claude (opt-out respected).
    r = resolve_roles(ALL, use_codex=False, use_kimi=False)
    results["opt_out_respected"] = (
        r["reviewer"].name == "claude" and r["tiebreaker"].name == "claude"
    )

    # 7. Round-trip: resolved plan -> .resolved.toml -> HarnessConfig backends.
    r = resolve_roles(ALL, use_codex=True, use_kimi=True)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".resolved.toml"
        write_resolved(r, path)
        cfg = HarnessConfig.load(profile_path=None, resolved_path=path)
    results["config_roundtrip"] = (
        cfg.models.reviewer.name == "codex"
        and cfg.models.reviewer.fmt == "codex_jsonl"
        and CODEX in cfg.models.reviewer.cmd
        and cfg.models.reviewer.stdin is True
        and cfg.models.tiebreaker.name == "kimi"
        and KIMI in cfg.models.tiebreaker.cmd
        and cfg.models.tiebreaker.stdin is False
    )

    # 8. Default config (no resolved file) is the Claude-only floor.
    cfg = HarnessConfig.load(profile_path=None,
                             resolved_path=Path("/nonexistent/.resolved.toml"))
    results["default_is_claude_floor"] = (
        cfg.models.reviewer.name == "claude"
        and cfg.models.tiebreaker.name == "claude"
        and cfg.models.reviewer.cmd[0] == "claude"
        # the floor must be read-only too (F1): tools disabled by default
        and "--tools" in cfg.models.reviewer.cmd
        and "--tools" in cfg.models.tiebreaker.cmd
    )

    # 9. Field-wise overlay (F5): a partial override keeps omitted fields from
    #    the lower-precedence layer instead of resetting them to Claude defaults.
    cfg = HarnessConfig()
    cfg._apply({"models": {"reviewer": {
        "name": "codex", "cmd": [CODEX, "exec"], "fmt": "codex_jsonl", "stdin": True}}})
    cfg._apply({"models": {"reviewer": {"cmd": [CODEX, "exec", "--json"]}}})  # only cmd
    results["fieldwise_overlay"] = (
        cfg.models.reviewer.name == "codex"          # preserved
        and cfg.models.reviewer.fmt == "codex_jsonl"  # preserved
        and cfg.models.reviewer.cmd == [CODEX, "exec", "--json"]  # overridden
    )

    # 10. TOML escaping (F4): a path with a quote/backslash renders to VALID toml.
    b = Backend("codex", ['/weird/pa"th\\x', "exec"], "codex_jsonl", stdin=True)
    rendered = "[models.reviewer]\n" + b.as_toml_table()
    parsed = tomllib.loads(rendered)  # must not raise
    results["toml_escaping"] = (parsed["models"]["reviewer"]["cmd"][0] == '/weird/pa"th\\x')

    # 11. Claude fallback is read-only (F1): tools disabled via `--tools ""`.
    r = resolve_roles(CLAUDE_ONLY, use_codex=True, use_kimi=True)
    cmd = r["reviewer"].cmd
    results["claude_reviewer_read_only"] = (
        "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    )

    # 12. Detection ignores non-executable known paths (F3).
    import os as _os
    with tempfile.TemporaryDirectory() as d:
        from harness.resolve import _find
        nonexec = Path(d) / "codex"          # exists but not executable
        nonexec.write_text("#!/bin/sh\n")
        _os.chmod(nonexec, 0o644)
        miss = _find("____nope____", [str(nonexec)])
        execbin = Path(d) / "kimi"
        execbin.write_text("#!/bin/sh\n")
        _os.chmod(execbin, 0o755)
        hit = _find("____nope____", [str(execbin)])
    results["detect_requires_executable"] = (miss is None and hit == str(execbin))

    ok = True
    for name, passed in results.items():
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
