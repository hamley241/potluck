"""Tests for `real_run_gate` — the PRODUCTION gate reader.

Why this file exists: `real_run_gate` had ZERO coverage. Every test in
`test_orchestrator.py` injects a stub gate callable, so the whole suite ran
green while the real function was broken end to end — a merge had left it
referencing a `proc` variable that a prior refactor to
`run_subprocess_result` had removed, so EVERY call raised NameError
regardless of gate outcome. Nothing failed until a live run pointed potluck
at a real repo.

That is observation 004's disease in the live system: a green wider than its
evidence. Stubs prove the ORCHESTRATOR's branching; only this file proves the
function that reads an actual gate. Both are needed and neither substitutes.

These tests run a real bash script through the real subprocess path. No
mocks: the point is the seam the stubs cannot reach.
"""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile

from harness.orchestrator import GateResult, real_run_gate


def _gate_script(tmpdir: str, body: str) -> str:
    """Write an executable gate script and return its path."""
    path = os.path.join(tmpdir, "verify.sh")
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


async def _run(body: str) -> GateResult:
    with tempfile.TemporaryDirectory() as tmp:
        raw = await real_run_gate(_gate_script(tmp, body))
    return GateResult.from_json_str(raw)


def _check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'' if cond else '  ' + detail}")
    return cond


async def main() -> bool:
    ok = True

    # A gate that PASSES: ok=True, stdout carried, exit_code 0.
    r = await _run('echo "580 passed"\nexit 0\n')
    ok &= _check("gate_pass_ok_true", r.ok is True, f"ok={r.ok!r}")
    ok &= _check("gate_pass_exit_code_0", r.exit_code == 0, f"exit_code={r.exit_code!r}")
    ok &= _check("gate_pass_carries_stdout", "580 passed" in r.output, repr(r.output[:60]))

    # A gate that RAN and FAILED (exit 1): ok=False, exit_code 1 -- the
    # orchestrator reads this as "the code failed the gate".
    r = await _run('echo "1 failed" >&2\nexit 1\n')
    ok &= _check("gate_fail_ok_false", r.ok is False, f"ok={r.ok!r}")
    ok &= _check("gate_fail_exit_code_1", r.exit_code == 1, f"exit_code={r.exit_code!r}")

    # A gate that COULD NOT RUN (exit 2): ok=False AND exit_code 2, which is
    # what lets the orchestrator route it to no-signal instead of "code
    # failed" (menu drift #13's convention).
    r = await _run('echo "no .venv" >&2\nexit 2\n')
    ok &= _check("gate_cannot_run_ok_false", r.ok is False, f"ok={r.ok!r}")
    ok &= _check("gate_cannot_run_exit_code_2", r.exit_code == 2, f"exit_code={r.exit_code!r}")

    # A signal-killed gate returns a NEGATIVE code and must never read as a
    # silent pass.
    r = await _run('kill -TERM $$\nsleep 5\n')
    ok &= _check("gate_signal_killed_not_ok", r.ok is False, f"ok={r.ok!r}")
    ok &= _check("gate_signal_killed_negative", r.exit_code is not None and r.exit_code < 0,
                 f"exit_code={r.exit_code!r}")

    # Every case above went through GateResult.from_json_str(), so the
    # str-typed StepResult.output boundary is exercised by construction.

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
