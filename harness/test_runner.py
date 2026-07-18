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

from harness.runner import run_subprocess, _drain


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


async def _chatty_child_survives_grace_window() -> bool:
    """A SIGTERM-trapping child that flushes >= 256 KB from its handler must
    exit CLEANLY within the grace window, not eat SIGKILL. This is the exact
    "flush logs and exit" case the grace window exists for.

    Pre-fix this fails: communicate() is cancelled, so nobody reads the child's
    stdout; the handler blocks in write() once the ~64 KB pipe buffer fills,
    never reaches exit, the grace expires, and it gets SIGKILLed -- leaving the
    sentinel empty. Post-fix the teardown drains the pipe concurrently, so the
    handler finishes flushing and writes 'clean' before exiting."""
    sentinel = tempfile.NamedTemporaryFile(delete=False)
    sentinel.close()
    # 256 KB is larger than any default pipe buffer, so a handler that flushes
    # it wedges on write() unless someone is draining the read end. The handler
    # records its PID alongside 'clean' so the test can also assert the process
    # actually exited (drained-and-exited, not drained-but-lingering).
    script = (
        "import os, sys, signal, time\n"
        "def handler(signum, frame):\n"
        "    sys.stdout.write('x' * (256 * 1024))\n"
        "    sys.stdout.flush()\n"
        "    open(sys.argv[1], 'w').write('clean ' + str(os.getpid()))\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, handler)\n"
        "time.sleep(30)\n"
    )
    cmd = [sys.executable, "-c", script, sentinel.name]

    try:
        try:
            await asyncio.wait_for(run_subprocess(cmd), timeout=1.0)
            return False  # it should NOT have completed
        except asyncio.TimeoutError:
            pass  # expected -- triggers the SIGTERM+grace teardown

        # Let the handler flush + exit within the grace window and the reap
        # settle.
        await asyncio.sleep(1.0)
        parts = open(sentinel.name).read().strip().split()
        if len(parts) != 2 or parts[0] != "clean":
            return False
        # The child flushed cleanly AND the process is gone -- it exited via the
        # handler within the grace window rather than being SIGKILLed.
        return _pid_dead(int(parts[1]))
    finally:
        # The sentinel is delete=False (the child, a separate process, writes
        # it) so we remove it here rather than leaking one tempfile per run.
        try:
            os.unlink(sentinel.name)
        except OSError:
            pass


async def _invalid_utf8_stdout_replaced() -> bool:
    """A backend emitting invalid UTF-8 on stdout must not raise
    UnicodeDecodeError (a ValueError that slips through StepRunner's
    (RuntimeError, OSError) catch). Strict decode -> errors='replace'.

    Asserts the U+FFFD replacement character actually lands in the returned
    string: errors='ignore' would silently drop the bad bytes and also not
    raise, so a bare "isinstance str" check passes under either policy. Pinning
    U+FFFD is what fails the test if the decode ever regresses to 'ignore'."""
    cmd = [sys.executable, "-c",
           "import sys; sys.stdout.buffer.write(b'\\xff\\xfe'); sys.exit(0)"]
    try:
        out = await run_subprocess(cmd)
    except UnicodeDecodeError:
        return False  # the bug: strict decode crashed the success path
    return isinstance(out, str) and "�" in out


async def _invalid_utf8_stderr_message_builds() -> bool:
    """Non-zero exit with invalid UTF-8 on stderr: the RuntimeError message
    interpolates stderr, so a strict decode there raises UnicodeDecodeError
    while building the exception. Must surface as RuntimeError, not decode.

    Asserts the U+FFFD replacement character is in the RuntimeError message:
    errors='ignore' would build the message without raising too, so only
    pinning U+FFFD distinguishes 'replace' from 'ignore'."""
    cmd = [sys.executable, "-c",
           "import sys; sys.stderr.buffer.write(b'\\xff\\xfe'); sys.exit(1)"]
    try:
        await run_subprocess(cmd)
    except RuntimeError as e:
        return "�" in str(e)  # error path reported cleanly, bytes replaced
    except UnicodeDecodeError:
        return False  # the bug: message interpolation crashed
    return False      # a non-zero exit must raise SOMETHING


async def _drain_survives_transient_concurrent_reader() -> bool:
    """_drain must survive a transient concurrent-reader RuntimeError and still
    drain to EOF. communicate()'s cancelled reader can still hold the stream on
    the first read (StreamReader rejects a concurrent reader with RuntimeError);
    the sleep(0) at the call site narrows but does not close that window under a
    loaded loop, so _drain retries rather than abandoning the pipe -- else the
    original wedge-on-full-pipe failure re-opens.

    Fake stream: read() raises RuntimeError once, then returns data, then b""
    (EOF). Pins that _drain retried past the RuntimeError AND consumed to EOF."""
    class FakeStream:
        def __init__(self):
            self.calls = 0
            self.consumed = []
            # First call: transient concurrent-reader error. Then a data chunk,
            # then EOF.
            self._script = [RuntimeError("read() called while another"
                                        " coroutine is already waiting"),
                            b"buffered output",
                            b""]

        async def read(self, n):
            self.calls += 1
            item = self._script[self.calls - 1] if self.calls <= len(
                self._script) else b""
            if isinstance(item, RuntimeError):
                raise item
            self.consumed.append(item)
            return item

    fake = FakeStream()
    await _drain(fake)
    # Retried past the RuntimeError (>=3 calls: error, data, EOF) and consumed
    # the real data before hitting EOF.
    return fake.calls >= 3 and fake.consumed == [b"buffered output", b""]


def main():
    results = {}
    results["child_process_killed_on_timeout"] = asyncio.run(
        _process_killed_on_timeout())
    # A well-behaved chatty child (flushes >=256 KB from its SIGTERM handler)
    # must survive the grace window instead of being SIGKILLed mid-flush.
    results["chatty_child_survives_grace_window"] = asyncio.run(
        _chatty_child_survives_grace_window())
    # Arbitrary byte output must never raise UnicodeDecodeError on either path.
    results["invalid_utf8_stdout_replaced"] = asyncio.run(
        _invalid_utf8_stdout_replaced())
    results["invalid_utf8_stderr_message_builds"] = asyncio.run(
        _invalid_utf8_stderr_message_builds())
    # _drain retries past a transient concurrent-reader RuntimeError (bounded)
    # and still drains to EOF -- pins Member B1.
    results["drain_survives_transient_concurrent_reader"] = asyncio.run(
        _drain_survives_transient_concurrent_reader())
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
