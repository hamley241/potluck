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
    return _pid_dead(pid)


def _pid_dead(pid: int) -> bool:
    """True iff pid is gone (or a zombie we can't observe as alive)."""
    try:
        os.kill(pid, 0)      # signal 0 = existence check
        return False         # still alive -> leak -> fail
    except ProcessLookupError:
        return True          # gone -> reaped correctly
    except PermissionError:
        return False         # alive but not ours -> treat as fail


async def _grandchild_dies_on_timeout(ignore_sigterm: bool) -> bool:
    """Child forks a grandchild (double-fork so it escapes the parent's
    direct-child slot). Both must be gone after cancellation. Optional
    SIGTERM ignore exercises the SIGKILL fallback path."""
    child_pidfile = tempfile.NamedTemporaryFile(delete=False)
    child_pidfile.close()
    grand_pidfile = tempfile.NamedTemporaryFile(delete=False)
    grand_pidfile.close()

    ignore_setup = ""
    if ignore_sigterm:
        # Grandchild ignores SIGTERM -- forces the SIGKILL fallback path.
        ignore_setup = "signal.signal(signal.SIGTERM, signal.SIG_IGN);"

    script = f"""
import os, sys, time, signal
# Child writes its own PID, then double-forks a grandchild.
open(sys.argv[1], 'w').write(str(os.getpid()))
pid = os.fork()
if pid == 0:
    # Grandchild
    {ignore_setup}
    open(sys.argv[2], 'w').write(str(os.getpid()))
    time.sleep(30)
    sys.exit(0)
# Child waits so both processes are alive when the timeout hits.
time.sleep(30)
"""
    cmd = [sys.executable, "-c", script, child_pidfile.name, grand_pidfile.name]

    try:
        await asyncio.wait_for(run_subprocess(cmd), timeout=1.0)
        return False
    except asyncio.TimeoutError:
        pass

    # Give the two-phase teardown (SIGTERM + grace + SIGKILL) time to complete.
    await asyncio.sleep(1.5)
    child_pid = int((open(child_pidfile.name).read().strip() or "0"))
    grand_pid = int((open(grand_pidfile.name).read().strip() or "0"))
    if child_pid <= 0 or grand_pid <= 0:
        return False
    return _pid_dead(child_pid) and _pid_dead(grand_pid)


def main():
    results = {}
    results["child_process_killed_on_timeout"] = asyncio.run(
        _process_killed_on_timeout())
    # Both teardown paths: (a) grandchild respects SIGTERM (dies in grace
    # window); (b) grandchild ignores SIGTERM (SIGKILL fallback kicks in).
    # In both, the whole process group must be gone.
    results["grandchild_dies_via_sigterm"] = asyncio.run(
        _grandchild_dies_on_timeout(ignore_sigterm=False))
    results["grandchild_dies_via_sigkill"] = asyncio.run(
        _grandchild_dies_on_timeout(ignore_sigterm=True))

    ok = True
    for name, passed in results.items():
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
