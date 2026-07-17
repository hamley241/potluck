"""Exercise the control logic with stubbed models -- no real CLI calls.

We are testing the HARNESS contracts (deterministic, no model intelligence
required), exactly the kind of thing that should be testable without a model:
  - early exit on clean review
  - debate convergence when reviewer concedes
  - tiebreaker resolving a deadlock (2-of-3)
  - escalation on genuine unresolved disagreement
  - timeout escalation (consecutive same-step)
  - gate failure escalation
"""

import asyncio
import json

from harness.config import HarnessConfig
from harness.orchestrator import (
    Orchestrator, DoerClient, ReviewerClient, TiebreakerClient,
)
from harness.schemas import DoerResponse, IssueResponse, Outcome


# --- stubs ---

class StubDoer(DoerClient):
    def __init__(self, decisions):  # decisions: {issue_id: "accept"|"reject"}
        self.decisions = decisions
    async def implement(self, spec, acceptance): return "implemented"
    async def respond_to_review(self, spec, verdict):
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision=self.decisions.get(i.id, "accept"),
                          reasoning="stub")
            for i in verdict.issues
        ])
    async def apply_fixes(self, ids): return "applied"


class StubReviewer(ReviewerClient):
    def __init__(self, cfg, first, followups=None):
        super().__init__(cfg)
        self.first = first
        self.followups = followups or []
        self._n = 0
    async def review(self, spec, diff): return json.dumps(self.first)
    async def respond(self, spec, diff, rejections):
        out = self.followups[self._n] if self._n < len(self.followups) else {"issues": []}
        self._n += 1
        return json.dumps(out)


class StubTiebreaker(TiebreakerClient):
    def __init__(self, cfg, sides):  # sides: {issue_id: "a"|"b"|"unclear"}
        super().__init__(cfg)
        self.sides = sides
    async def adjudicate(self, spec, diff, issue_id, a, b):
        return json.dumps({"id": issue_id,
                           "sides_with": self.sides.get(issue_id, "unclear"),
                           "reasoning": "stub"})


class RecordingTiebreaker(TiebreakerClient):
    """Records what actually reached adjudicate() so tests can assert on the
    real arguments -- catches regressions like passing '<doer-arg>' placeholder
    strings instead of the real doer/reviewer reasoning."""
    def __init__(self, cfg, sides):
        super().__init__(cfg)
        self.sides = sides
        self.calls: list[dict] = []
    async def adjudicate(self, spec, diff, issue_id, a, b):
        self.calls.append({"id": issue_id, "a": a, "b": b})
        return json.dumps({"id": issue_id,
                           "sides_with": self.sides.get(issue_id, "unclear"),
                           "reasoning": "stub"})


class ErrorReviewer(ReviewerClient):
    """Reviewer whose CLI errors (e.g. unauthenticated) -- must escalate as
    'no signal', never be treated as approval."""
    async def review(self, spec, diff):
        raise RuntimeError("codex exited 1: not logged in")


class ErrorTiebreaker(TiebreakerClient):
    """Tiebreaker whose CLI errors -- must escalate as 'no signal', distinct
    from a genuine disagreement (F1)."""
    async def adjudicate(self, spec, diff, issue_id, a, b):
        raise RuntimeError("kimi exited 1: not authenticated")


async def gate_pass(): return (True, "all green")
async def gate_fail(): return (False, "")
async def gate_hang():
    await asyncio.sleep(10)  # longer than the tiny test timeout
    return (True, "green")
async def diff_security(): return "modified src/security/phi_crypto.py"
async def diff_plain(): return "modified src/feature.py"


def make(cfg, doer, reviewer, tb, gate, diff, ab_swap=None):
    # adapt run_gate to return just ok+output the orchestrator expects
    async def run_gate():
        ok, out = await gate()
        return out if ok else ""
    # Default: no swap -> A=doer, B=reviewer. Deterministic so tests can encode
    # "kimi sides with doer" as sides_with="a" without also asserting on the
    # (real-run) random A/B position.
    return Orchestrator(cfg, doer, reviewer, tb, run_gate, diff,
                        ab_swap=ab_swap or (lambda _id: False))


async def main():
    results = {}

    # 1. Early exit: reviewer finds nothing blocking.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["early_exit"] = (r.outcome, r.rounds_used)

    # 2. Convergence: one blocking issue, author rejects, reviewer concedes.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": []}])  # reviewer drops it
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["convergence"] = (r.outcome, r.rounds_used)

    # 3. Tiebreaker resolves: deadlock, Kimi sides with doer -> passes.
    # With the default no-swap ab_swap, A=doer, so sides_with="a" == doer wins.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]},  # holds
                   {"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]}])
    tb = StubTiebreaker(cfg, {"I1": "a"})
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, tb, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["tiebreak_resolves"] = (r.outcome, r.rounds_used)

    # 4. Escalation: deadlock, tiebreaker unclear -> escalate to human.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]}] * 3)
    tb = StubTiebreaker(cfg, {"I1": "unclear"})
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, tb, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["escalate_disagreement"] = (r.outcome, r.rounds_used)

    # 5. Timeout escalation: gate hangs repeatedly (tiny timeout to force it).
    cfg = HarnessConfig()
    cfg.timeouts.gate_seconds = 0  # force immediate timeout
    cfg.escalation.retries_before_counting = 0
    cfg.escalation.consecutive_same_step_threshold = 2
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_hang, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["timeout_escalation"] = (r.outcome, r.rounds_used)

    # 6. Gate failure escalation.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_fail, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["gate_failure"] = (r.outcome, r.rounds_used)

    # 7. Human-only path routing (debate skipped -> human review).
    cfg = HarnessConfig()
    cfg.human_only_paths = ["src/security/"]
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_security)
    r = await orch.run_feature("spec", "acc")
    results["human_only_routing"] = (r.outcome, r.rounds_used)

    # 8. CI profile: debate disabled -> stops at green gate.
    cfg = HarnessConfig()
    cfg.debate_enabled = False
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["ci_gate_only"] = (r.outcome, r.rounds_used)

    # 9. Reviewer CLI errors (unauthenticated) -> no signal -> escalate.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), ErrorReviewer(cfg), None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["reviewer_no_signal"] = (r.outcome, r.rounds_used)

    # 10. Tiebreaker CLI errors on a real deadlock -> no signal (NOT disagreement).
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]}] * 3)
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer,
                ErrorTiebreaker(cfg), gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["tiebreaker_no_signal"] = (r.outcome, r.rounds_used)

    # 11. INVARIANT: real doer & reviewer arguments reach the tiebreaker.
    # Regression guard for the bug where _tiebreak passed literal
    # "<doer-arg>", "<reviewer-arg>" placeholder strings -- so Kimi was judging
    # empty prompts. If this ever regresses, the tiebreaker silently loses its
    # inputs while the loop keeps "working".
    class RejectingDoer(StubDoer):
        async def respond_to_review(self, spec, verdict):
            return DoerResponse(responses=[
                IssueResponse(id="I1", decision="reject",
                              reasoning="doer-rejection-text")
            ])
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "reviewer-issue-text",
                           "suggested_fix": "reviewer-fix-text"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "reviewer-issue-text",
                                "suggested_fix": "reviewer-fix-text"}]}] * 3)
    tb = RecordingTiebreaker(cfg, {"I1": "a"})  # A wins; with no-swap A=doer
    orch = make(cfg, RejectingDoer({}), reviewer, tb, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert len(tb.calls) == 1, f"expected 1 tiebreak call, got {len(tb.calls)}"
    call = tb.calls[0]
    combined = call["a"] + call["b"]
    assert "<doer-arg>" not in combined, \
        f"placeholder leaked to tiebreaker: a={call['a']!r}, b={call['b']!r}"
    assert "<reviewer-arg>" not in combined, \
        f"placeholder leaked to tiebreaker: a={call['a']!r}, b={call['b']!r}"
    # With default no-swap: A=doer, B=reviewer.
    assert call["a"] == "doer-rejection-text", \
        f"expected doer reasoning in slot A, got {call['a']!r}"
    assert "reviewer-issue-text" in call["b"], \
        f"expected reviewer issue text in slot B, got {call['b']!r}"
    assert "reviewer-fix-text" in call["b"], \
        f"expected reviewer suggested_fix in slot B, got {call['b']!r}"
    results["invariant_real_args"] = (r.outcome, r.rounds_used)

    # 12. INVARIANT: TiebreakVerdict schema is A/B, not doer/reviewer.
    # The tiebreaker is blind to which model authored which argument; asking it
    # for a role label is asking it to guess. The schema enforces slot answers;
    # the orchestrator does the role translation. If this ever regresses to
    # accepting role labels, blinding degrades to "aspirational".
    from harness.schemas import TiebreakVerdict  # noqa: E402
    from pydantic import ValidationError  # noqa: E402
    for legacy in ("doer", "reviewer"):
        try:
            TiebreakVerdict(id="I1", sides_with=legacy, reasoning="stub")
        except ValidationError:
            pass  # expected
        else:
            raise AssertionError(
                f"TiebreakVerdict must reject legacy sides_with={legacy!r}")
    for valid in ("a", "b", "unclear"):
        TiebreakVerdict(id="I1", sides_with=valid, reasoning="stub")

    # 13. INVARIANT: full debate transcript present in log with new event
    # shapes. Regression guard for the whole point of this feature: reviewer
    # issue text, per-issue doer reasoning, held-issue text, and tiebreak
    # positions must all be readable from FeatureResult.debate_log without
    # re-running. Also enforces the v2 dropped-field discipline: accepted/
    # rejected/still_blocking must NOT appear on their respective v2 events.
    class DoerWithDistinctiveText(StubDoer):
        async def respond_to_review(self, spec, verdict):
            return DoerResponse(responses=[
                IssueResponse(id="I1", decision="reject",
                              reasoning="DOER_REASONING_MARKER")
            ])
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "REVIEWER_ISSUE_MARKER",
                           "suggested_fix": "REVIEWER_FIX_MARKER"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "REVIEWER_HELD_MARKER",
                                "suggested_fix": "y"}]}] * 3)
    # Use sides_with="a" so a regression that logs the raw slot label would
    # produce a literal "a" in the emitted event. sides_with="unclear" is a
    # blind spot -- it survives even the buggy "leak slots" behavior.
    class StubTiebreakerWithReasoning(TiebreakerClient):
        async def adjudicate(self, spec, diff, issue_id, a, b):
            return json.dumps({"id": issue_id, "sides_with": "a",
                               "reasoning": "TB_REASONING_MARKER"})
    tb = StubTiebreakerWithReasoning(cfg)
    orch = make(cfg, DoerWithDistinctiveText({}), reviewer, tb,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    log = r.debate_log
    # Every event has envelope fields.
    for ev in log:
        assert "event" in ev, f"event missing 'event': {ev}"
        assert "event_version" in ev, f"event missing 'event_version': {ev}"
        assert "ts" in ev, f"event missing 'ts': {ev}"
        assert isinstance(ev["event_version"], int)
        assert isinstance(ev["ts"], str) and "T" in ev["ts"], \
            f"ts must be ISO 8601: {ev['ts']!r}"
    # `review` event carries the reviewer's issues verbatim.
    reviews = [e for e in log if e["event"] == "review"]
    assert len(reviews) == 1, f"expected 1 review event, got {len(reviews)}"
    rev = reviews[0]
    assert rev["event_version"] == 1
    assert any(i["issue"] == "REVIEWER_ISSUE_MARKER" for i in rev["issues"]), \
        f"review event missing issue text: {rev['issues']}"
    # `doer_response` v2 carries per-issue reasoning; no accepted/rejected lists.
    doer_events = [e for e in log if e["event"] == "doer_response"]
    assert doer_events, "expected at least one doer_response event"
    for de in doer_events:
        assert de["event_version"] == 2, \
            f"doer_response must be v2: {de['event_version']}"
        assert "accepted" not in de, \
            f"v2 doer_response must not emit 'accepted' field: {de}"
        assert "rejected" not in de, \
            f"v2 doer_response must not emit 'rejected' field: {de}"
        assert "responses" in de, "v2 doer_response must have 'responses'"
    assert any(r["reasoning"] == "DOER_REASONING_MARKER"
               for de in doer_events for r in de["responses"]), \
        "doer reasoning text missing from log"
    # `reviewer_followup` v2 carries held_issues; no still_blocking id list.
    followups = [e for e in log if e["event"] == "reviewer_followup"]
    assert followups, "expected at least one reviewer_followup event"
    for fu in followups:
        assert fu["event_version"] == 2
        assert "still_blocking" not in fu, \
            f"v2 reviewer_followup must not emit 'still_blocking': {fu}"
        assert "held_issues" in fu
    assert any(i["issue"] == "REVIEWER_HELD_MARKER"
               for fu in followups for i in fu["held_issues"]), \
        "reviewer held-issue text missing from log"
    # `tiebreak` v2 carries doer_position/reviewer_position/tb_reasoning;
    # slot labels (arg_a/arg_b) never leak into the log.
    tiebreaks = [e for e in log if e["event"] == "tiebreak"]
    assert tiebreaks, "expected at least one tiebreak event"
    for tbe in tiebreaks:
        assert tbe["event_version"] == 2
        # No slot artifacts of any name.
        for banned in ("arg_a", "arg_b", "sides_with"):
            assert banned not in tbe, \
                f"tiebreak must not leak slot key '{banned}': {tbe}"
        # No slot VALUES either -- a future regression could rename the field
        # but still store "a"/"b". Guard against that too.
        assert tbe.get("winning_role") not in ("a", "b"), \
            f"tiebreak winning_role must be semantic role, not slot: {tbe}"
        assert "doer_position" in tbe
        assert "reviewer_position" in tbe
        assert "tb_reasoning" in tbe
        assert tbe["doer_position"] == "DOER_REASONING_MARKER"
        assert "REVIEWER_HELD_MARKER" in tbe["reviewer_position"]
        assert tbe["tb_reasoning"] == "TB_REASONING_MARKER"
        # winning_role must be a semantic role (or "unclear").
        assert tbe["winning_role"] in ("doer", "reviewer", "unclear"), \
            f"winning_role must be semantic: {tbe['winning_role']}"
    # held_issues must be filtered to actual holds. Reviewer stub above returns
    # only I1 (blocking) in followups, and doer rejects I1, so held_issues in
    # each followup event must contain exactly I1 and no stray issues.
    for fu in followups:
        held_ids = {i["id"] for i in fu["held_issues"]}
        assert held_ids <= {"I1"}, \
            f"held_issues must be filtered to actual holds, got {held_ids}"
    results["invariant_transcript"] = (r.outcome, r.rounds_used)

    # 14a. INVARIANT: _pack_event rejects invalid event_version. A compliant
    # consumer drops unknown versions, so 0/negative/bool/non-int would make
    # the event silently disappear at the reader. Fail loud at emit instead.
    from harness.orchestrator import _pack_event, _truncate_walk  # noqa: E402
    for bad in (0, -1, "1", 1.0, True, False, None):
        try:
            _pack_event("test", bad, foo="bar")
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(
                f"_pack_event must reject event_version={bad!r}")
    # Sanity: valid version accepted.
    _pack_event("test", 1, foo="bar")

    # 14b. INVARIANT: _truncate_walk survives cycles and pathological depth
    # without RecursionError. Logging must never hard-fail; the walker either
    # descends or emits a depth-truncation sentinel.
    cycle: list = []
    cycle.append(cycle)  # self-referential list
    packed = _pack_event("test", 1, cyclic=cycle)  # must not raise
    assert "cyclic" in packed
    # Same for a very deep dict.
    deep: dict = {}
    cur = deep
    for _ in range(200):
        cur["nested"] = {}
        cur = cur["nested"]
    _pack_event("test", 1, deep=deep)  # must not raise

    # 14. INVARIANT: strings above 16KB cap are truncated and metadata is
    # emitted at event-level `truncations`. Field type stays str (no
    # str|dict unions) so consumers don't have to branch on type.
    big = "X" * (20 * 1024)  # 20KB, well above the 16KB cap
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "minor",
                           "issue": big, "suggested_fix": "y"}]})
    orch = make(cfg, StubDoer({}), reviewer, None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    review_events = [e for e in r.debate_log if e["event"] == "review"]
    assert review_events, "expected review event even on early exit"
    rev = review_events[0]
    assert "truncations" in rev, \
        f"expected truncations sibling on oversized event: {rev}"
    # Path is dotted+bracketed: issues[0].issue for the truncated field.
    assert "issues[0].issue" in rev["truncations"], \
        f"expected path 'issues[0].issue' in truncations: {rev['truncations']}"
    assert rev["truncations"]["issues[0].issue"] == 20 * 1024, \
        f"truncations must record original length: {rev['truncations']}"
    truncated_issue = rev["issues"][0]["issue"]
    assert isinstance(truncated_issue, str), \
        f"truncated field must stay str, not union: {type(truncated_issue)}"
    assert len(truncated_issue) == 16 * 1024, \
        f"truncated str must be exactly cap chars: {len(truncated_issue)}"
    results["invariant_truncation"] = (r.outcome, r.rounds_used)

    # report
    expected = {
        "early_exit": (Outcome.PASSED, 0),
        "convergence": (Outcome.PASSED, 1),
        "tiebreak_resolves": (Outcome.PASSED, 2),
        "escalate_disagreement": (Outcome.ESCALATED_DISAGREEMENT, 2),
        "timeout_escalation": (Outcome.ESCALATED_TIMEOUT, 0),
        "gate_failure": (Outcome.ESCALATED_GATE, 0),
        "human_only_routing": (Outcome.ESCALATED_DISAGREEMENT, 0),
        "ci_gate_only": (Outcome.PASSED, 0),
        "reviewer_no_signal": (Outcome.ESCALATED_NO_SIGNAL, 0),
        "tiebreaker_no_signal": (Outcome.ESCALATED_NO_SIGNAL, 0),
        "invariant_real_args": (Outcome.PASSED, 2),
        "invariant_transcript": (Outcome.PASSED, 2),
        "invariant_truncation": (Outcome.PASSED, 0),
    }
    ok = True
    for name, got in results.items():
        exp = expected[name]
        passed = got == exp
        ok = ok and passed
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name:24} got={got[0].value},{got[1]}  exp={exp[0].value},{exp[1]}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
