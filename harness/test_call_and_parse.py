"""Pin the five model-call-and-parse boundaries that now route through the
ONE shared helper `Orchestrator._call_and_parse` (slice B1).

This slice is a PURE REFACTOR: it adds no retry and changes no behaviour. So
these tests assert the OBSERVABLES the helper must preserve bit-for-bit --
across malformed / timeout / not-ok / happy-path replies -- for every boundary:

  * `_review`, `_review_followup`, `_doer_respond_to_review`, `_tiebreak`
    ESCALATE (raise TimeoutEscalation / ModelUnavailable). Each raise's role,
    pattern, and MESSAGE is asserted LITERALLY (the prefixes and framing are
    copied verbatim from orchestrator.py) so a single reworded string here
    fails the test -- these strings are what the escalation report shows a human.
  * `_closure_sweep` is the ONE boundary whose timeout/not-ok/malformed outcomes
    LOG a `closure_skipped` and return None instead of raising (an advisory
    add-on must never turn a good run into an escalation). It routes through the
    same helper via its own on_timeout/on_not_ok/on_malformed handlers; we
    assert its ACTUAL logged behaviour, not a raise it never performed.

The runner is stubbed (a StubRunner returns a chosen StepResult per step, or
executes the coroutine pass-through), so no real model or CLI is involved --
the same contract-testing discipline the rest of the suite uses.

SA-7 (both directions, probe evidence): each run prints the resolved
`__file__` of the modules it exercised, so "this would fail against the broken
tree" is EVIDENCED by which files were loaded, not assumed.
"""

import asyncio
import json

import harness.orchestrator as orchestrator
from harness.config import HarnessConfig
from harness.orchestrator import (
    Orchestrator,
    _bounded_payload,
    _parse_verdict,
    _parse_tiebreak,
    _parse_closure,
)
import harness_core.runner as core_runner
from harness_core.runner import StepResult, TimeoutEscalation, ModelUnavailable
from harness.schemas import (
    ReviewVerdict, ReviewIssue, DoerResponse, IssueResponse, ClosureReport,
)


# --- stubs -----------------------------------------------------------------

class _Client:
    """One stub standing in for the doer/reviewer/tiebreaker. The StubRunner
    returns a preset StepResult without invoking these, so the bodies are never
    reached for the escalation cases; they exist so the wrappers' `call`
    closures reference real attributes and so `_prompt_log` can be attached."""
    async def review(self, *a, **k): return ""
    async def respond(self, *a, **k): return ""
    async def closure_scan(self, *a, **k): return ""
    async def respond_to_review(self, *a, **k): return DoerResponse(responses=[])
    async def adjudicate(self, *a, **k): return ""


class StubRunner:
    """Replaces StepRunner. For any step_name in `injected`, returns the preset
    StepResult (so a test can force timed_out / not-ok / a specific output);
    otherwise executes the coroutine and wraps it ok (pass-through -- used for
    the closure sweep's `closure_grep` step, which is not the boundary here)."""
    def __init__(self, injected):
        self.injected = injected
        self.calls: list[str] = []

    async def run(self, step_name, coro_factory, timeout_seconds):
        self.calls.append(step_name)
        if step_name in self.injected:
            return self.injected[step_name]
        out = await coro_factory()
        return StepResult(ok=True, output=out)


def _make(cfg=None, ab_swap=None):
    cfg = cfg or HarnessConfig()

    async def _noop():
        return ""

    orch = Orchestrator(cfg, _Client(), _Client(), _Client(),
                        _noop, _noop, ab_swap=ab_swap or (lambda _id: False))
    return orch


# The verdict/response context _tiebreak needs to reach the adjudicate call:
# I1 must be a held reviewer issue AND a rejected doer response.
_VERDICT = ReviewVerdict(issues=[
    ReviewIssue(id="I1", severity="blocking", issue="x", suggested_fix="y")])
_REJECTED = DoerResponse(responses=[
    IssueResponse(id="I1", decision="reject", reasoning="doer-reasoning")])

# A malformed reply every parser rejects (no JSON at all).
_BAD = "this is not json at all"


def _expected_malformed(prefix, parser):
    """Rebuild the EXACT ModelUnavailable detail the helper produces for a
    malformed reply. `prefix` and the ` | payload (...)` framing are copied
    verbatim from orchestrator.py; the `{e}` middle is obtained by running the
    SAME parser on the SAME bytes, so equality pins the whole message while
    tolerating the parser library's own wording."""
    try:
        parser(_BAD)
    except (ValueError, RecursionError) as e:
        body, plen = _bounded_payload(_BAD)
        return f"{prefix}{e} | payload ({plen} chars): {body}"
    raise AssertionError(f"{parser!r} did not reject the malformed reply")


# --- the four ESCALATING boundaries ----------------------------------------
# Each row: (name, step, role, timeout_pattern, timeout_detail,
#            not_ok_detail, malformed_prefix, parser, invoke, happy_output,
#            happy_check)

async def _invoke_review(orch):
    return await orch._review("spec", "acc", "diff")


async def _invoke_followup(orch):
    return await orch._review_followup("spec", "acc", "diff", [])


async def _invoke_doer(orch):
    return await orch._doer_respond_to_review("spec", "acc", "diff", _VERDICT)


async def _invoke_tiebreak(orch):
    return await orch._tiebreak("spec", "acc", "diff", {"I1"},
                               _VERDICT, _REJECTED, 1)


_ESCALATING = [
    dict(name="review", step="reviewer:review", role="reviewer",
         tpat="reviewer_no_signal",
         tdetail="reviewer timed out; refusing to proceed without review",
         notok="reviewer call errored",
         prefix="reviewer returned malformed response: ",
         parser=_parse_verdict, invoke=_invoke_review,
         happy=json.dumps({"issues": [{"id": "I1", "severity": "blocking",
                                       "issue": "x", "suggested_fix": "y"}]}),
         happy_check=lambda v: isinstance(v, ReviewVerdict)
         and [i.id for i in v.issues] == ["I1"]),
    dict(name="review_followup", step="reviewer:followup", role="reviewer",
         tpat="reviewer_no_signal", tdetail="reviewer follow-up timed out",
         notok="reviewer follow-up errored",
         prefix="reviewer returned malformed response: ",
         parser=_parse_verdict, invoke=_invoke_followup,
         happy=json.dumps({"issues": []}),
         happy_check=lambda v: isinstance(v, ReviewVerdict) and v.issues == []),
    dict(name="doer_respond", step="doer:respond", role="doer",
         tpat="doer_no_signal",
         tdetail="doer timed out during respond_to_review",
         notok="doer respond errored",
         prefix="doer returned malformed response: ",
         parser=DoerResponse.model_validate_json, invoke=_invoke_doer,
         happy=json.dumps({"responses": [{"id": "I1", "decision": "accept",
                                          "reasoning": "ok"}]}),
         happy_check=lambda r: isinstance(r, DoerResponse)
         and [x.id for x in r.responses] == ["I1"]),
    # _tiebreak's TIMEOUT does NOT raise -- it leaves the issue contested and
    # returns it still blocking. not-ok and malformed DO raise, like the others.
    dict(name="tiebreak", step="tiebreaker:adjudicate", role="tiebreaker",
         tpat=None, tdetail=None,           # timeout handled specially below
         notok="tiebreaker call errored",
         prefix="tiebreaker returned malformed response: ",
         parser=_parse_tiebreak, invoke=_invoke_tiebreak,
         # sides_with="a" with no A/B swap => slot A is the doer => doer wins =>
         # I1 resolves and drops out => returns an empty still-blocking set.
         happy=json.dumps({"id": "I1", "sides_with": "a", "reasoning": "r"}),
         happy_check=lambda s: s == set()),
]


async def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # SA-7 probe: name the exact module files these assertions exercise, so a
    # "fails against the broken tree" claim is evidenced by the loaded paths.
    print(f"[probe] orchestrator under test: {orchestrator.__file__}")
    print(f"[probe] escalations from       : {core_runner.__file__}")

    for b in _ESCALATING:
        # 1. malformed -> ModelUnavailable, exact role + literal message.
        orch = _make()
        orch.runner = StubRunner({b["step"]: StepResult(ok=True, output=_BAD)})
        raised = None
        try:
            await b["invoke"](orch)
        except BaseException as e:  # noqa: BLE001 -- capture the exact escape
            raised = e
        want = _expected_malformed(b["prefix"], b["parser"])
        check(f"{b['name']}: malformed -> ModelUnavailable",
              isinstance(raised, ModelUnavailable))
        check(f"{b['name']}: malformed role={b['role']!r}",
              getattr(raised, "role", None) == b["role"])
        check(f"{b['name']}: malformed message literal",
              getattr(raised, "detail", None) == want)

        # 3. not-ok (no error text) -> ModelUnavailable with the LITERAL
        #    per-boundary fallback detail.
        orch = _make()
        orch.runner = StubRunner(
            {b["step"]: StepResult(ok=False, error=None)})
        raised = None
        try:
            await b["invoke"](orch)
        except BaseException as e:  # noqa: BLE001
            raised = e
        check(f"{b['name']}: not-ok -> ModelUnavailable",
              isinstance(raised, ModelUnavailable))
        check(f"{b['name']}: not-ok detail literal",
              getattr(raised, "detail", None) == b["notok"])

        # 4. happy path -> the parsed object flows back out.
        orch = _make()
        orch.runner = StubRunner(
            {b["step"]: StepResult(ok=True, output=b["happy"])})
        result = await b["invoke"](orch)
        check(f"{b['name']}: happy path returns parsed", b["happy_check"](result))

    # 2. Timeout, per boundary -- distinct pattern/detail, asserted literally.
    for b, tpat, tdetail in (
        (_ESCALATING[0], "reviewer_no_signal",
         "reviewer timed out; refusing to proceed without review"),
        (_ESCALATING[1], "reviewer_no_signal", "reviewer follow-up timed out"),
        (_ESCALATING[2], "doer_no_signal",
         "doer timed out during respond_to_review"),
    ):
        orch = _make()
        orch.runner = StubRunner(
            {b["step"]: StepResult(ok=False, timed_out=True, error="x")})
        raised = None
        try:
            await b["invoke"](orch)
        except BaseException as e:  # noqa: BLE001
            raised = e
        check(f"{b['name']}: timeout -> TimeoutEscalation",
              isinstance(raised, TimeoutEscalation))
        check(f"{b['name']}: timeout pattern={tpat!r}",
              getattr(raised, "pattern", None) == tpat)
        check(f"{b['name']}: timeout detail literal",
              getattr(raised, "detail", None) == tdetail)

    # _tiebreak timeout: NOT a raise. The issue stays contested (still blocking).
    orch = _make()
    orch.runner = StubRunner(
        {"tiebreaker:adjudicate": StepResult(ok=False, timed_out=True, error="x")})
    result = await _invoke_tiebreak(orch)
    check("tiebreak: timeout keeps issue blocking (no raise)", result == {"I1"})

    # --- the closure sweep: LOG-and-skip, never raises -----------------------
    # timeout -> closure_skipped(reason="model_timeout"), returns None.
    orch = _make()
    orch.runner = StubRunner(
        {"closure": StepResult(ok=False, timed_out=True, error="x")})
    result = await orch._closure_sweep("spec", "diff")
    skips = [e for e in orch.log if e["event"] == "closure_skipped"]
    check("closure: timeout returns None", result is None)
    check("closure: timeout logs reason='model_timeout'",
          len(skips) == 1 and skips[0].get("reason") == "model_timeout")

    # not-ok (no error text) -> closure_skipped with the LITERAL fallback reason.
    orch = _make()
    orch.runner = StubRunner({"closure": StepResult(ok=False, error=None)})
    result = await orch._closure_sweep("spec", "diff")
    skips = [e for e in orch.log if e["event"] == "closure_skipped"]
    check("closure: not-ok returns None", result is None)
    check("closure: not-ok logs reason='closure model call errored'",
          len(skips) == 1 and skips[0].get("reason") == "closure model call errored")

    # malformed -> closure_skipped(reason="malformed", payload=<full reply>).
    orch = _make()
    orch.runner = StubRunner({"closure": StepResult(ok=True, output=_BAD)})
    result = await orch._closure_sweep("spec", "diff")
    skips = [e for e in orch.log if e["event"] == "closure_skipped"]
    check("closure: malformed returns None", result is None)
    check("closure: malformed logs reason='malformed' + payload",
          len(skips) == 1 and skips[0].get("reason") == "malformed"
          and skips[0].get("payload") == _BAD)

    # happy -> a valid reply parses; the sweep proceeds to a ClosureReport
    # (empty patterns -> no grep, no candidates) with NO closure_skipped(malformed).
    orch = _make()
    orch.runner = StubRunner({"closure": StepResult(
        ok=True, output=json.dumps({"bug_class": "none", "patterns": []}))})
    result = await orch._closure_sweep("spec", "diff")
    malformed_skips = [e for e in orch.log if e["event"] == "closure_skipped"
                       and e.get("reason") == "malformed"]
    check("closure: happy path returns a ClosureReport",
          isinstance(result, ClosureReport) and result.bug_class == "none")
    check("closure: happy path logs no malformed skip", malformed_skips == [])

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
