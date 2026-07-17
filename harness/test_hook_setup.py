"""Exercise the secret-scan hook + its registration.

Two surfaces:
  1. `base/hooks/pre-tool-secret-scan.py` (the tripwire itself) -- black-box
     tested by running it as a subprocess with stdin payloads and asserting
     on exit code + stderr. Testing the deployed shape catches issues that
     an in-process import wouldn't (config lookup from CWD, exit codes,
     stderr formatting).
  2. `harness/hook_setup.register` (the settings.json wiring) -- unit tested
     directly since it's pure Python operating on a filesystem path.

The point of these tests is not to prove regex correctness (that's what the
patterns document); it's to prove the CONTROLS work: PII is on by default,
whitelist stops the common test-fixture false positives, off-switch requires
a committed config line and only affects PII (secrets stay on).
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from harness.hook_setup import register


HOOK_SCRIPT = (
    Path(__file__).parent.parent / "base" / "hooks" / "pre-tool-secret-scan.py"
)


async def _run_hook(payload: str, cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(HOOK_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate(input=payload.encode())
    return proc.returncode, stdout.decode(), stderr.decode()


# --- Hook tests --------------------------------------------------------------

async def _clean_payload_passes() -> bool:
    with tempfile.TemporaryDirectory() as td:
        rc, _, _ = await _run_hook("hello world\nno secrets here\n", cwd=td)
        return rc == 0


async def _real_secret_blocks() -> bool:
    with tempfile.TemporaryDirectory() as td:
        rc, _, stderr = await _run_hook(
            "AKIA1234567890ABCDEF is my key\n", cwd=td)
        return rc != 0 and "SECRET" in stderr


async def _real_pii_blocks_by_default() -> bool:
    with tempfile.TemporaryDirectory() as td:
        # SSN-shaped that is NOT in the reserved test range (not 000-/666-/9##-).
        rc, _, stderr = await _run_hook(
            "user SSN is 123-99-1234\n", cwd=td)
        return rc != 0 and "PII" in stderr


async def _whitelisted_pii_does_not_block() -> bool:
    """Canonical test fixtures (RFC 2606 domains, Visa test PAN, SSA-reserved
    SSN) must not fire -- that's the whole point of the whitelist. Otherwise
    the hook breaks every test suite in the world that uses example.com."""
    with tempfile.TemporaryDirectory() as td:
        payload = (
            "email = user@example.com\n"
            "card = 4111 1111 1111 1111\n"
            "ssn = 000-12-3456\n"
            "example_ssn = 123-45-6789\n"
        )
        rc, _, _ = await _run_hook(payload, cwd=td)
        return rc == 0


async def _pii_off_switch_via_config() -> bool:
    """A committed `.claude/secret-scan.toml` with `pii_enabled = false`
    turns PII off (secrets still fire). Env-var opt-out was rejected on
    purpose -- opting out must be attributable and on-the-record."""
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td) / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "secret-scan.toml").write_text("pii_enabled = false\n")
        # A non-whitelisted SSN would normally block; with pii off it passes.
        rc_pii, _, _ = await _run_hook("SSN 111-22-3333\n", cwd=td)
        # But a real secret STILL blocks even with pii off.
        rc_secret, _, stderr_secret = await _run_hook(
            "AKIA1234567890ABCDEF\n", cwd=td)
        return (rc_pii == 0 and rc_secret != 0 and "SECRET" in stderr_secret)


# --- hook_setup.register tests -----------------------------------------------

def _register_adds_hook_when_settings_absent() -> bool:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        script = home / "hooks" / "pre-tool-secret-scan.py"
        result = register(home, script)
        settings = json.loads((home / "settings.json").read_text())
        entries = settings.get("hooks", {}).get("PreToolUse", [])
        return (result["status"] == "added"
                and len(entries) == 1
                and entries[0]["hooks"][0]["command"] == str(script))


def _register_is_idempotent() -> bool:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        script = home / "hooks" / "pre-tool-secret-scan.py"
        register(home, script)
        result = register(home, script)  # second call
        settings = json.loads((home / "settings.json").read_text())
        entries = settings.get("hooks", {}).get("PreToolUse", [])
        # Second registration must be a no-op AND leave exactly one entry.
        return result["status"] == "already_present" and len(entries) == 1


def _register_preserves_unrelated_hooks() -> bool:
    """Merge-semantics test: an existing hook from another tool must survive
    potluck's registration. Otherwise setup silently clobbers user config
    and the README's promise that we merge (not replace) is false."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        other = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/opt/other/hook"}],
                }],
                "SomethingElse": ["preserved"],
            },
            "unrelated_top_level": "must survive",
        }
        (home / "settings.json").write_text(json.dumps(other))
        script = home / "hooks" / "pre-tool-secret-scan.py"
        register(home, script)
        settings = json.loads((home / "settings.json").read_text())
        pretool = settings["hooks"]["PreToolUse"]
        commands = [h["hooks"][0]["command"] for h in pretool]
        return (
            "/opt/other/hook" in commands
            and str(script) in commands
            and settings["hooks"]["SomethingElse"] == ["preserved"]
            and settings["unrelated_top_level"] == "must survive"
        )


def _register_refuses_malformed_settings() -> bool:
    """A malformed settings.json is a stop-and-ask situation, not
    silent-overwrite -- refuse to edit rather than clobber a file we can't
    parse."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / "settings.json").write_text("not-json-at-all")
        script = home / "hooks" / "pre-tool-secret-scan.py"
        try:
            register(home, script)
            return False
        except RuntimeError:
            return True


def _register_refuses_wrong_shape_hooks_key() -> bool:
    """If some other tool wrote `{"hooks": [...]}` (list instead of dict),
    the previous code would `.setdefault("PreToolUse", [])` on a list and
    raise AttributeError -- callers only catch RuntimeError, so setup would
    abort with a stack trace instead of a clean refusal message."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / "settings.json").write_text(json.dumps({"hooks": []}))
        script = home / "hooks" / "pre-tool-secret-scan.py"
        try:
            register(home, script)
            return False  # should have raised
        except RuntimeError:
            return True
        except AttributeError:
            return False  # unclean crash -- exactly the bug we're guarding


def _register_refuses_wrong_shape_pretool_key() -> bool:
    """Similarly for `{"hooks": {"PreToolUse": "not a list"}}` -- must be a
    clean RuntimeError, not an unhandled AttributeError from setdefault."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": "oops"}}))
        script = home / "hooks" / "pre-tool-secret-scan.py"
        try:
            register(home, script)
            return False
        except RuntimeError:
            return True
        except AttributeError:
            return False


def _register_write_is_atomic() -> bool:
    """After registration, no temp-file leftovers should sit next to
    settings.json -- atomic write via tempfile + os.replace, with cleanup on
    exception path. Absence of stragglers is what the atomic-write plumbing
    is for; regressions would leave `.tmp` files behind."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        script = home / "hooks" / "pre-tool-secret-scan.py"
        register(home, script)
        leftovers = [p.name for p in home.iterdir()
                     if p.name != "settings.json"]
        return leftovers == []


def main() -> bool:
    results: dict[str, bool] = {}
    results["hook_clean_payload_passes"] = asyncio.run(_clean_payload_passes())
    results["hook_real_secret_blocks"] = asyncio.run(_real_secret_blocks())
    results["hook_real_pii_blocks_by_default"] = asyncio.run(
        _real_pii_blocks_by_default())
    results["hook_whitelisted_pii_does_not_block"] = asyncio.run(
        _whitelisted_pii_does_not_block())
    results["hook_pii_off_switch_via_config"] = asyncio.run(
        _pii_off_switch_via_config())
    results["register_adds_hook_when_settings_absent"] = (
        _register_adds_hook_when_settings_absent())
    results["register_is_idempotent"] = _register_is_idempotent()
    results["register_preserves_unrelated_hooks"] = (
        _register_preserves_unrelated_hooks())
    results["register_refuses_malformed_settings"] = (
        _register_refuses_malformed_settings())
    results["register_refuses_wrong_shape_hooks_key"] = (
        _register_refuses_wrong_shape_hooks_key())
    results["register_refuses_wrong_shape_pretool_key"] = (
        _register_refuses_wrong_shape_pretool_key())
    results["register_write_is_atomic"] = _register_write_is_atomic()

    ok = True
    for name, passed in results.items():
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
