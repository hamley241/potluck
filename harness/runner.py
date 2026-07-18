"""Bounded execution: every external wait is time-boxed, and repeated hangs
feed an escalation counter.

Design points we agreed on:
- A timeout is a normal failure that flows through StepResult, not a special path.
- One timeout -> retry once transparently (transient provider slowness is common).
- Repeated timeouts -> escalate, distinguishing two patterns:
    * N total timeouts within a feature (scattered slowness), OR
    * the same step timing out K times in a row (usually a broken environment).
- A timeout is NEVER a verdict. A timed-out review is "no signal", never
  "approved" or "rejected".
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections import defaultdict

from .config import HarnessConfig
from .schemas import StepResult

# How long to give the process group to exit gracefully after SIGTERM before
# escalating to SIGKILL. Short enough that a hung process doesn't delay
# escalation reporting; long enough that a well-behaved child can flush logs
# and clean up before we force-kill.
_GRACE_SECONDS = 0.5


class TimeoutEscalation(Exception):
    """Raised when repeated timeouts cross a threshold. Carries the pattern
    that fired so the escalation report can say *why*."""

    def __init__(self, pattern: str, detail: str):
        self.pattern = pattern      # "total_count" | "consecutive_same_step"
        self.detail = detail
        super().__init__(f"timeout escalation ({pattern}): {detail}")


class ModelUnavailable(Exception):
    """Raised when a required external signal source ERRORS (not times out).
    The archetype is a model call -- the CLI is missing, unauthenticated, or
    exits non-zero -- but the same applies to the deterministic gate callable
    (verify.sh missing, an OSError/RuntimeError out of run_gate, or a
    malformed GateResult): an errored gate produced NO verdict about the code.
    Like a timeout, all of these are *no signal*, never a verdict: we escalate
    rather than treat an errored reviewer as approval or an errored gate as a
    gate failure."""

    def __init__(self, role: str, detail: str):
        self.role = role
        self.detail = detail
        super().__init__(f"model unavailable ({role}): {detail}")


class StepRunner:
    """Runs bounded steps and tracks timeout patterns across one feature."""

    def __init__(self, config: HarnessConfig):
        self.cfg = config
        self._total_timeouts = 0
        self._consecutive: dict[str, int] = defaultdict(int)

    async def run(
        self,
        step_name: str,
        coro_factory,
        timeout_seconds: int,
    ) -> StepResult:
        """Run an awaitable produced by coro_factory() under a wall-clock bound.

        coro_factory is a zero-arg callable returning a fresh awaitable, so we
        can re-invoke it cleanly on retry.
        """
        attempts = self.cfg.escalation.retries_before_counting + 1
        last_result: StepResult | None = None

        for attempt in range(attempts):
            try:
                output = await asyncio.wait_for(coro_factory(), timeout=timeout_seconds)
                # Success resets the consecutive-timeout streak for this step.
                self._consecutive[step_name] = 0
                return StepResult(ok=True, output=output)
            except asyncio.TimeoutError:
                last_result = StepResult(
                    ok=False,
                    timed_out=True,
                    error=f"{step_name} exceeded {timeout_seconds}s",
                    recovery_hint=(
                        "Step did not complete in time. If this is the gate, "
                        "inspect the environment (hung server, flaky test). "
                        "If a model call, the provider may be slow."
                    ),
                )
                # Transparent retry on the first timeout; only count if it sticks.
                if attempt < attempts - 1:
                    continue
                self._register_timeout(step_name)
            except (RuntimeError, OSError) as e:
                # The call ERRORED (CLI missing / unauthenticated / non-zero
                # exit), which is distinct from a timeout. Not retried and not
                # counted as a timeout -- surfaced as a non-ok result so the
                # caller can escalate it as "no signal".
                return StepResult(
                    ok=False,
                    timed_out=False,
                    error=f"{step_name} errored: {e}",
                    recovery_hint=(
                        "External call failed to run. Check the model CLI is "
                        "installed and authenticated (`potluck doctor`)."
                    ),
                )

        return last_result  # type: ignore[return-value]

    def _register_timeout(self, step_name: str) -> None:
        self._total_timeouts += 1
        self._consecutive[step_name] += 1

        esc = self.cfg.escalation
        if self._consecutive[step_name] >= esc.consecutive_same_step_threshold:
            raise TimeoutEscalation(
                "consecutive_same_step",
                f"'{step_name}' timed out {self._consecutive[step_name]} times in a row "
                f"-- likely an environment problem, not a transient one.",
            )
        if self._total_timeouts >= esc.timeout_count_threshold:
            raise TimeoutEscalation(
                "total_count",
                f"{self._total_timeouts} timeouts accumulated this feature "
                f"across steps -- providers or environment are unreliable right now.",
            )


async def _drain(stream: asyncio.StreamReader) -> None:
    """Read a stream to EOF, discarding everything. Used during the SIGTERM
    grace window so a child flushing buffered output doesn't wedge on a full
    pipe. Any error (the transport tearing down under us as the process dies)
    is swallowed -- this is best-effort cleanup on an already-cancelled call.

    RuntimeError from `stream.read` is special-cased: communicate()'s cancelled
    reader may still hold the stream when we start (StreamReader rejects a
    concurrent reader with RuntimeError). The sleep(0) at the call site yields
    once to let it unwind, but a single yield is not a guarantee under a loaded
    loop, so we retry a few times here -- yielding again between attempts --
    before giving up. Once a read succeeds we fall back into the normal
    drain-to-EOF loop; a transient concurrent-reader error must not end the
    drain permanently and re-open the wedge-on-full-pipe failure."""
    _MAX_RUNTIME_RETRIES = 3
    attempts = 0
    try:
        while True:
            try:
                chunk = await stream.read(65536)
            except RuntimeError:
                attempts += 1
                if attempts > _MAX_RUNTIME_RETRIES:
                    return
                # The concurrent reader hasn't released the stream yet; yield
                # and retry rather than abandoning the drain.
                await asyncio.sleep(0)
                continue
            if not chunk:
                return  # EOF
    except Exception:
        pass


async def _cancel_drainers(drainers: list) -> None:
    """Cancel and await the background pipe-reader tasks so none leaks past
    run_subprocess. Awaited on both the graceful-exit and SIGKILL paths."""
    for d in drainers:
        d.cancel()
    for d in drainers:
        try:
            await d
        except (asyncio.CancelledError, Exception):
            pass


async def run_subprocess(cmd: list[str], stdin_text: str | None = None) -> str:
    """Run an external CLI, return stdout. Raises on non-zero exit.

    No timeout here on purpose -- the StepRunner wraps the call and owns the
    wall-clock bound, so timeout policy lives in exactly one place.

    Spawns each child in its own POSIX process group (via
    `start_new_session=True`, i.e. setsid() in the child) so cancellation
    can kill the WHOLE group. Model CLIs often fork helper processes --
    killing only the direct child would leak grandchildren that continue
    running (burning tokens, holding locks), and a retry would spawn a
    second copy of the work.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # New session -> distinct process group (so the killpg teardown
        # below reaches the whole tree). start_new_session is the documented,
        # thread-safe spelling of "run setsid() in the child" -- the older
        # fork-hook form is unsafe in the presence of threads and deprecated
        # for exactly this use.
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate(
            input=stdin_text.encode() if stdin_text is not None else None
        )
    except BaseException:
        # On cancellation (the StepRunner's wait_for timing us out) the child
        # would otherwise keep running -- leaking a process and burning model
        # tokens in the background, and a retry would spawn a second one.
        # Two-phase teardown: SIGTERM the group first, give it a grace window
        # to flush logs and exit cleanly, then SIGKILL if still alive.
        # SIGKILL-only would guarantee lost final output; SIGTERM-only would
        # never end if a child ignores it.
        if proc.returncode is None:
            pgid = None
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pass
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                # communicate() was cancelled, so nobody is reading the
                # child's pipes anymore. A well-behaved child that flushes
                # buffered output from its SIGTERM handler -- the exact case
                # this grace window exists for -- blocks in write() the moment
                # the ~64 KB pipe buffer fills, never reaches exit, and eats
                # SIGKILL. Drain both pipes concurrently with the wait so the
                # child can finish flushing and exit cleanly. The bytes are
                # discarded: the call is already cancelled, there is no
                # consumer. The readers are cleaned up on BOTH paths below so
                # no task leaks past this function.
                #
                # Yield the loop once before spawning the drainers.
                # communicate()'s internal per-stream reader tasks were
                # cancelled when this coroutine was cancelled, but may not have
                # unwound yet; StreamReader rejects a concurrent reader with
                # RuntimeError, which _drain would otherwise hit on its first
                # read. This sleep(0) yields so those cancelled readers can
                # release the streams in the common case. A single yield is not
                # a guarantee -- under a loaded loop the readers may need more
                # than one turn to unwind -- so _drain itself retries on the
                # concurrent-reader RuntimeError rather than giving up. No claim
                # of ready-queue-ordering independence: the yield narrows the
                # window, the retry closes it.
                await asyncio.sleep(0)
                drainers = [
                    asyncio.ensure_future(_drain(stream))
                    for stream in (proc.stdout, proc.stderr)
                    if stream is not None
                ]
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    # Grace expired; child ignored SIGTERM or grandchildren
                    # kept the group alive. Force-kill the whole group.
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                finally:
                    await _cancel_drainers(drainers)
            else:
                # Couldn't get pgid (child already exited between check and
                # kill); fall through to plain wait.
                await proc.wait()
        raise
    # Strict decode of arbitrary backend bytes raises UnicodeDecodeError (a
    # ValueError), which slips through StepRunner's (RuntimeError, OSError)
    # catch and crashes the run. errors="replace" keeps the boundary total.
    if proc.returncode != 0:
        raise RuntimeError(
            f"command {cmd[0]} exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')[:500]}"
        )
    return stdout.decode(errors="replace")
