"""Exercise run_subprocess's cancellation cleanup (F2 from the harness review).

When a model call is timed out by the StepRunner (asyncio.wait_for), the spawned
child process must be killed and reaped -- otherwise it keeps running, leaks a
process, and a retry spawns a second one that keeps burning tokens.

This is the one test that spawns a real subprocess (a short Python sleeper), so
it lives apart from the pure control-logic tests.
"""

import asyncio
import os
import sys
import tempfile

from harness.runner import run_subprocess


async def _process_killed_on_timeout() -> bool:
    # Child writes its PID immediately, then sleeps well past our timeout.
    pidfile = tempfile.NamedTemporaryFile(delete=False)
    pidfile.close()
    script = ("import os, sys, time; "
              "open(sys.argv[1], 'w').write(str(os.getpid())); "
              "time.sleep(30)")
    cmd = [sys.executable, "-c", script, pidfile.name]

    try:
        await asyncio.wait_for(run_subprocess(cmd), timeout=1.0)
        return False  # it should NOT have completed
    except asyncio.TimeoutError:
        pass  # expected

    await asyncio.sleep(0.3)  # let the kill+reap settle
    pid = int((open(pidfile.name).read().strip() or "0"))
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)      # signal 0 = existence check
        return False         # still alive -> leak -> fail
    except ProcessLookupError:
        return True          # gone -> reaped correctly
    except PermissionError:
        return False         # alive but not ours -> treat as fail


def main():
    ok = asyncio.run(_process_killed_on_timeout())
    print(f"  [{'PASS' if ok else 'FAIL'}] child_process_killed_on_timeout")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
