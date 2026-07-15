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
from collections import defaultdict

from .config import HarnessConfig
from .schemas import StepResult


class TimeoutEscalation(Exception):
    """Raised when repeated timeouts cross a threshold. Carries the pattern
    that fired so the escalation report can say *why*."""

    def __init__(self, pattern: str, detail: str):
        self.pattern = pattern      # "total_count" | "consecutive_same_step"
        self.detail = detail
        super().__init__(f"timeout escalation ({pattern}): {detail}")


class ModelUnavailable(Exception):
    """Raised when a required model call ERRORS (not times out) -- e.g. the CLI
    is missing, unauthenticated, or exits non-zero. Like a timeout, this is
    *no signal*, never a verdict: we escalate rather than treat an errored
    reviewer as approval."""

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


async def run_subprocess(cmd: list[str], stdin_text: str | None = None) -> str:
    """Run an external CLI, return stdout. Raises on non-zero exit.

    No timeout here on purpose -- the StepRunner wraps the call and owns the
    wall-clock bound, so timeout policy lives in exactly one place.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(
        input=stdin_text.encode() if stdin_text is not None else None
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command {cmd[0]} exited {proc.returncode}: {stderr.decode()[:500]}"
        )
    return stdout.decode()
