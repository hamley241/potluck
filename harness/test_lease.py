"""The lease (ruling 002 R3/R5): one attempt per admission, a 1200s implement.

No model calls. Covers:
  profiles -- every shipped profile loads, validates, and resolves potluck's
              lease values (retries 0, implement 1200, the restated feature_seconds).
  attempts -- under potluck's config one bounded call is ONE factory invocation on
              timeout; under a bare harness_core EscalationConfig it is still two.
  implement -- a stubbed implement taking >600s (<1200s) of simulated time
              completes, and the tree is not restored before it does.
  identity -- a resume whose only difference is retries_before_counting is refused.
  prompt   -- doer:implement emits prompt_size (and prompt_large above the
              threshold) for the exact bytes sent; the prompt text is unchanged.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from harness_core.config import EscalationConfig as CoreEscalationConfig
from harness_core.runner import StepRunner

from harness import checkpoint as ck
from harness import orchestrator as orch_mod
from harness.cli import _effective_config_dict
from harness.config import HarnessConfig
from harness.orchestrator import RealDoerClient
from harness.schemas import Outcome
from harness.test_orchestrator import (
    RecordingRestore, StubDoer, StubReviewer, diff_plain, gate_pass, make,
)

_PROFILES = Path(__file__).resolve().parent.parent / "profiles"
_NO_RESOLVED = Path("/nonexistent/potluck-test-lease.resolved.toml")


def _load(name: str) -> HarnessConfig:
    # resolved_path points nowhere so a machine-local .resolved.toml cannot leak in.
    return HarnessConfig.load(_PROFILES / name / "profile.toml",
                              resolved_path=_NO_RESOLVED)


class _SimClock:
    """Simulated wall-clock. `fake_wait_for` replaces asyncio.wait_for: it runs
    the awaitable to completion and reports a timeout iff the simulated time it
    consumed exceeds the bound -- so a 900s implement costs no real time."""
    def __init__(self):
        self.now = 0

    async def fake_wait_for(self, aw, timeout):
        start = self.now
        out = await aw
        if timeout is not None and self.now - start > timeout:
            raise asyncio.TimeoutError()
        return out


class _SlowImplementDoer(StubDoer):
    def __init__(self, clock, order, seconds):
        super().__init__({})
        self.clock, self.order, self.seconds = clock, order, seconds
        self.invocations = 0

    async def implement(self, spec, acceptance):
        self.invocations += 1
        self.order.append("attempt-start")
        self.clock.now += self.seconds
        self.order.append("attempt-done")
        return "implemented"


async def _run_slow_implement(implement_bound: int | None):
    clock, order = _SimClock(), []
    cfg = HarnessConfig()
    if implement_bound is not None:
        cfg.timeouts.doer_step_seconds = {**cfg.timeouts.doer_step_seconds,
                                          "doer:implement": implement_bound}
    restore = RecordingRestore(order)
    doer = _SlowImplementDoer(clock, order, seconds=900)
    o = make(cfg, doer, StubReviewer(cfg, {"issues": []}), None,
             gate_pass, diff_plain, restore_tree=restore)
    real_wait_for = asyncio.wait_for
    asyncio.wait_for = clock.fake_wait_for
    try:
        r = await o.run_feature("spec", "acc")
    finally:
        asyncio.wait_for = real_wait_for
    return r, order, doer


async def main() -> bool:
    results = {}

    # --- A2: potluck's default resolves one attempt per bounded call -----
    # Fails if HarnessConfig.escalation reverts to the core default (1).
    results["default_retries_0"] = (
        HarnessConfig().escalation.retries_before_counting == 0)

    # --- A3: the core default is untouched -------------------------------
    # Fails if someone "fixes" this by editing harness_core's default instead.
    results["core_default_still_1"] = (
        CoreEscalationConfig().retries_before_counting == 1)

    # --- A7: every shipped profile loads, validates, resolves the lease --
    # Fails if a profile's feature_seconds drops below outer_min (load() runs
    # validate() and raises), or a profile still sets retries_before_counting=1,
    # or implement is not 1200 after loading.
    for name in ("personal", "phi", "ci"):
        try:
            cfg = _load(name)
            cfg.validate()
            ok = (cfg.escalation.retries_before_counting == 0
                  and cfg.timeouts.resolve_doer_timeout("doer:implement") == 1200
                  and cfg.outer_min() <= cfg.timeouts.feature_seconds)
        except ValueError:
            ok = False
        results[f"profile_{name}_loads_validates"] = ok
    ci = _load("ci")
    # Fails if the ci profile's value is not the restated 2405 x 14.
    results["ci_feature_seconds_33670"] = (
        ci.timeouts.feature_seconds == 33670 and ci.outer_min() == 33670)
    for name in ("personal", "phi"):
        # Fails if personal/phi do not carry the restated 2405 x 30.
        results[f"profile_{name}_feature_seconds_72150"] = (
            _load(name).timeouts.feature_seconds == 72150)

    # --- A6: the restated outer bound at defaults ------------------------
    # Fails if implement leaves 1200 or feature_seconds is not restated to match.
    d = HarnessConfig()
    results["defaults_c_max_outer_min_feature"] = (
        d.timeouts.c_max() == 2405 and d.outer_min() == 72150
        and d.timeouts.feature_seconds == 72150)

    # --- A4: attempts per bounded call on timeout ------------------------
    async def _count(cfg_like):
        calls = 0
        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)  # always exceeds the bound below
        res = await StepRunner(cfg_like).run("doer:implement", factory, 0.01)
        return calls, res.timed_out
    # Fails if potluck's escalation config retries (calls would be 2).
    results["potluck_one_attempt_on_timeout"] = (
        await _count(HarnessConfig()) == (1, True))
    # Fails if harness_core's default changed (calls would be 1) -- proves the
    # one-attempt behaviour above comes from potluck's config, not the core.
    results["core_two_attempts_on_timeout"] = (
        await _count(SimpleNamespace(escalation=CoreEscalationConfig()))
        == (2, True))

    # --- A10: a >600s implement completes under the 1200s lease ----------
    r, order, doer = await _run_slow_implement(None)
    # Fails if doer:implement's bound is back at 600 (the 900s attempt times out,
    # tree_restored fires, and the outcome escalates), or if a restore runs
    # between the attempt starting and finishing.
    results["slow_implement_completes"] = (
        r.outcome == Outcome.PASSED
        and doer.invocations == 1
        and order == ["restore", "attempt-start", "attempt-done"]
        and not any(e["event"] == "tree_restored" for e in r.debate_log))
    # Control: the same 900s attempt under a 600s bound times out and restores.
    # Fails if the simulated clock stopped governing the bound -- which would
    # make the test above pass vacuously.
    r6, order6, doer6 = await _run_slow_implement(600)
    results["slow_implement_control_600_times_out"] = (
        r6.outcome == Outcome.ESCALATED_TIMEOUT
        and doer6.invocations == 1
        and any(e["event"] == "tree_restored" for e in r6.debate_log))

    # --- A9: retries_before_counting is a run commitment -----------------
    def _identity(cfg):
        digest = ck.config_digest(_effective_config_dict(cfg))
        mid = ck.compute_manifest_id("spec", "acc", digest, "base")
        return ck.build_identity("spec", "acc", digest, "base", "td", mid,
                                 "in-tree")
    committed_cfg = HarnessConfig()
    committed_cfg.escalation.retries_before_counting = 1  # the old policy
    resumed_cfg = HarnessConfig()                          # only this differs
    # Fails if retries_before_counting is dropped from _effective_config_dict.
    results["effective_config_has_retries"] = (
        _effective_config_dict(resumed_cfg).get("retries_before_counting") == 0)
    # Fails if the identity check stops binding the effective config, or the
    # retry policy is not part of it (the two identities would then be equal).
    refused_with = None
    try:
        ck.verify_identity(_identity(committed_cfg), _identity(resumed_cfg))
    except ck.CheckpointError as e:
        refused_with = str(e)
    results["resume_across_retry_flip_refused"] = (
        refused_with is not None and "effective config" in refused_with)
    # Fails if the identity is not deterministic (would refuse every resume).
    same_ok = True
    try:
        ck.verify_identity(_identity(resumed_cfg), _identity(HarnessConfig()))
    except ck.CheckpointError:
        same_ok = False
    results["resume_same_retry_policy_ok"] = same_ok

    # --- A11/A12: implement's prompt is measured, not gated --------------
    spec, acc = "spec é", "acc"
    expected_prompt = (
        f"Implement the following change.\n\n"
        f"SPEC:\n{spec}\n\n"
        f"ACCEPTANCE CRITERIA:\n{acc}\n\n"
        f"Make the SMALLEST SUFFICIENT change that satisfies the spec AND "
        f"the acceptance criteria: no speculative abstraction, no "
        f"unrequested surface, no refactoring beyond what the change needs. "
        f"Make the change now."
    )
    sent = []
    async def fake_run_subprocess(cmd, stdin_text=None):
        sent.append(stdin_text)
        return "implemented"
    real_run_subprocess = orch_mod.run_subprocess
    orch_mod.run_subprocess = fake_run_subprocess
    try:
        # Through the Orchestrator, so the doer is wired to the debate log.
        cfg = HarnessConfig()
        o = make(cfg, RealDoerClient(), StubReviewer(cfg, {"issues": []}), None,
                 gate_pass, diff_plain)
        rr = await o.run_feature(spec, acc)
        sizes = [e for e in rr.debate_log if e["event"] == "prompt_size"
                 and e.get("step") == "doer:implement"]
        # Fails if implement emits no prompt_size, measures something other than
        # the bytes sent (é makes chars != bytes), or the prompt text changed.
        results["implement_prompt_size_exact"] = (
            len(sent) == 1 and sent[0] == expected_prompt
            and len(sizes) == 1
            and sizes[0]["bytes"] == len(sent[0].encode("utf-8")))
        # Fails if prompt_large fires below the threshold.
        results["implement_no_prompt_large_below"] = not any(
            e["event"] == "prompt_large" and e.get("step") == "doer:implement"
            for e in rr.debate_log)

        # Above the threshold: prompt_large too, and the run is unchanged.
        cfg2 = HarnessConfig()
        cfg2.prompt.large_prompt_warn_bytes = 10
        o2 = make(cfg2, RealDoerClient(), StubReviewer(cfg2, {"issues": []}),
                  None, gate_pass, diff_plain)
        rr2 = await o2.run_feature(spec, acc)
        large = [e for e in rr2.debate_log if e["event"] == "prompt_large"
                 and e.get("step") == "doer:implement"]
        # Fails if implement's prompt_large is missing, or if the size now
        # gates/refuses/changes behaviour (outcome would differ from below).
        results["implement_prompt_large_above_records_only"] = (
            len(large) == 1 and large[0]["threshold"] == 10
            and rr2.outcome == rr.outcome == Outcome.PASSED
            and sent[1] == sent[0])
    finally:
        orch_mod.run_subprocess = real_run_subprocess

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
