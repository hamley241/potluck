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
    Orchestrator, DoerClient, ReviewerClient, TiebreakerClient, GateResult,
    DoerProtocolViolation, _parse_verdict, _extract_verdict_json,
)
from harness.schemas import DoerResponse, IssueResponse, Outcome


# --- stubs ---

class StubDoer(DoerClient):
    def __init__(self, decisions):  # decisions: {issue_id: "accept"|"reject"}
        self.decisions = decisions
        self.apply_fixes_calls: list[dict] = []
    async def implement(self, spec, acceptance): return "implemented"
    async def respond_to_review(self, spec, acceptance, diff, verdict):
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision=self.decisions.get(i.id, "accept"),
                          reasoning="stub")
            for i in verdict.issues
        ])
    async def apply_fixes(self, issues, diff):
        self.apply_fixes_calls.append(
            {"issues": [i.model_dump() for i in issues], "diff": diff})
        return "applied"


class EmptyResponseDoer(StubDoer):
    """Doer that returns an empty DoerResponse regardless of the verdict --
    emitting no signal on any reviewer issue. The loop must escalate rather
    than treat this as blanket acceptance (drift-11a)."""
    async def respond_to_review(self, spec, acceptance, diff, verdict):
        return DoerResponse(responses=[])


class PartialResponseDoer(StubDoer):
    """Doer that responds to only the FIRST verdict issue, omitting the rest --
    a strict-subset (partial) protocol violation."""
    async def respond_to_review(self, spec, acceptance, diff, verdict):
        first = verdict.issues[:1]
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision=self.decisions.get(i.id, "reject"),
                          reasoning="stub")
            for i in first
        ])


class StubReviewer(ReviewerClient):
    def __init__(self, cfg, first, followups=None):
        super().__init__(cfg)
        self.first = first
        self.followups = followups or []
        self._n = 0
    async def review(self, spec, acceptance, diff): return json.dumps(self.first)
    async def respond(self, spec, acceptance, diff, rejections):
        out = self.followups[self._n] if self._n < len(self.followups) else {"issues": []}
        self._n += 1
        return json.dumps(out)


class RawFollowupReviewer(StubReviewer):
    """Followup reviewer that returns its followup entries as raw model output
    (verbatim strings, not json.dumps'd) so tests can feed a literal top-level
    array like "[]" straight through respond() -> _parse_verdict."""
    async def respond(self, spec, acceptance, diff, rejections):
        out = self.followups[self._n] if self._n < len(self.followups) else "[]"
        self._n += 1
        return out


class StubTiebreaker(TiebreakerClient):
    def __init__(self, cfg, sides):  # sides: {issue_id: "a"|"b"|"unclear"}
        super().__init__(cfg)
        self.sides = sides
    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b):
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
    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b):
        self.calls.append({"id": issue_id, "a": a, "b": b,
                           "spec": spec, "acceptance": acceptance, "diff": diff})
        return json.dumps({"id": issue_id,
                           "sides_with": self.sides.get(issue_id, "unclear"),
                           "reasoning": "stub"})


class ErrorReviewer(ReviewerClient):
    """Reviewer whose CLI errors (e.g. unauthenticated) -- must escalate as
    'no signal', never be treated as approval."""
    async def review(self, spec, acceptance, diff):
        raise RuntimeError("codex exited 1: not logged in")


class ErrorTiebreaker(TiebreakerClient):
    """Tiebreaker whose CLI errors -- must escalate as 'no signal', distinct
    from a genuine disagreement (F1)."""
    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b):
        raise RuntimeError("kimi exited 1: not authenticated")


class ProseReviewer(ReviewerClient):
    """Reviewer that answers with prose containing no JSON at all. The parse
    raises ValueError, which run_feature does not catch -- must convert to a
    clean no-signal escalation, not an uncaught traceback."""
    async def review(self, spec, acceptance, diff):
        return "The diff looks fine, nothing to report."
    async def respond(self, spec, acceptance, diff, rejections):
        return "Still fine."


class MalformedTiebreaker(TiebreakerClient):
    """Tiebreaker that answers with prose (no JSON). Distinct from
    ErrorTiebreaker (whose CLI raises): here the call succeeds but the OUTPUT
    is unparseable, so the parse -- not the runner -- would crash. Must escalate
    the whole run, like the errored case, not leave the issue contested."""
    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b):
        return "I think the doer is probably right here."


async def gate_pass(): return (True, "all green")
async def gate_pass_silent(): return (True, "")  # exit 0 but no stdout
async def gate_fail(): return (False, "")
async def gate_hang():
    await asyncio.sleep(10)  # longer than the tiny test timeout
    return (True, "green")
async def diff_security():
    return ("diff --git a/src/security/phi_crypto.py b/src/security/phi_crypto.py\n"
            "@@ -1 +1 @@\n-old\n+new\n")
async def diff_plain():
    return ("diff --git a/src/feature.py b/src/feature.py\n"
            "@@ -1 +1 @@\n-old\n+new\n")
# Comment mentioning a sensitive path but no diff header touching it.
# Previously (substring match) this would have wrongly triggered human-only
# routing; the path-aware check must not fire.
async def diff_comment_only():
    return ("diff --git a/src/notes.py b/src/notes.py\n"
            "@@ -1 +1 @@\n-old\n+# see src/security/ for details\n")
# File renamed OUT of a sensitive tree: `a/` is under human-only, `b/` isn't.
# Either-side rule: this MUST trigger human-only.
async def diff_rename_out_of_sensitive():
    return ("diff --git a/src/security/legacy.py b/src/util/legacy.py\n"
            "similarity index 100%\nrename from src/security/legacy.py\n"
            "rename to src/util/legacy.py\n")


def make(cfg, doer, reviewer, tb, gate, diff, ab_swap=None, restore_tree=None):
    # Adapt the test gate fixtures (which return (ok, out)) into the real
    # orchestrator contract: () -> GateResult.to_json_str().
    async def run_gate():
        ok, out = await gate()
        return GateResult(ok=ok, output=out).to_json_str()
    # Default: no swap -> A=doer, B=reviewer. Deterministic so tests can encode
    # "kimi sides with doer" as sides_with="a" without also asserting on the
    # (real-run) random A/B position. restore_tree defaults to None -> the
    # Orchestrator's no-op, keeping every existing construction unchanged.
    return Orchestrator(cfg, doer, reviewer, tb, run_gate, diff,
                        restore_tree=restore_tree,
                        ab_swap=ab_swap or (lambda _id: False))


class RecordingRestore:
    """Records each restore_tree() call and the interleaving with doer attempts
    via a shared `order` list, so tests can assert the per-attempt hook fired
    before every implement attempt."""
    def __init__(self, order=None):
        self.calls = 0
        self.order = order if order is not None else []
    async def __call__(self):
        self.calls += 1
        self.order.append("restore")


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
        async def respond_to_review(self, spec, acceptance, diff, verdict):
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
    # Spec, acceptance, and diff all reached the tiebreaker (regression guard
    # for the bug where acceptance was silently dropped from reviewer/tiebreak
    # prompts even though it was passed to run_feature).
    assert call["spec"] == "spec", f"spec not threaded: {call['spec']!r}"
    assert call["acceptance"] == "acc", \
        f"acceptance not threaded: {call['acceptance']!r}"
    assert "src/feature.py" in call["diff"], \
        f"diff not threaded: {call['diff']!r}"
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
        async def respond_to_review(self, spec, acceptance, diff, verdict):
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
        async def adjudicate(self, spec, acceptance, diff, issue_id, a, b):
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
    # `tiebreak` v3 carries doer_position/reviewer_position/tb_reasoning
    # plus the sides_with+swap audit trail behind winning_role. `arg_a`/`arg_b`
    # are the OLD prompt-side slot labels that never returned; they must
    # still not leak.
    tiebreaks = [e for e in log if e["event"] == "tiebreak"]
    assert tiebreaks, "expected at least one tiebreak event"
    for tbe in tiebreaks:
        assert tbe["event_version"] == 3
        for banned in ("arg_a", "arg_b"):
            assert banned not in tbe, \
                f"tiebreak must not leak slot key '{banned}': {tbe}"
        # winning_role stays the semantic answer -- must never be a slot letter.
        assert tbe.get("winning_role") not in ("a", "b"), \
            f"tiebreak winning_role must be semantic role, not slot: {tbe}"
        assert "doer_position" in tbe
        assert "reviewer_position" in tbe
        assert "tb_reasoning" in tbe
        assert tbe["doer_position"] == "DOER_REASONING_MARKER"
        assert "REVIEWER_HELD_MARKER" in tbe["reviewer_position"]
        assert tbe["tb_reasoning"] == "TB_REASONING_MARKER"
        assert tbe["winning_role"] in ("doer", "reviewer", "unclear"), \
            f"winning_role must be semantic: {tbe['winning_role']}"
        # Audit trail: sides_with + swap must be present and typed correctly.
        # Together they let a reader reconstruct the translation without
        # re-running -- the reason we can put slot labels back in the log
        # is that the mapping now travels with them.
        assert "sides_with" in tbe, f"v3 tiebreak must emit sides_with: {tbe}"
        assert tbe["sides_with"] in ("a", "b", "unclear"), \
            f"sides_with must be raw slot answer: {tbe['sides_with']}"
        assert "swap" in tbe, f"v3 tiebreak must emit swap: {tbe}"
        assert isinstance(tbe["swap"], bool), \
            f"swap must be bool: {tbe['swap']!r}"
        # Translation invariant: winning_role is a pure function of
        # (sides_with, swap). Any regression in the a/b -> doer/reviewer
        # mapping shows up here.
        if tbe["sides_with"] == "unclear":
            assert tbe["winning_role"] == "unclear"
        else:
            role_of_a = "reviewer" if tbe["swap"] else "doer"
            role_of_b = "doer" if tbe["swap"] else "reviewer"
            expected = role_of_a if tbe["sides_with"] == "a" else role_of_b
            assert tbe["winning_role"] == expected, (
                f"winning_role {tbe['winning_role']} disagrees with "
                f"(sides_with={tbe['sides_with']}, swap={tbe['swap']}) "
                f"-> {expected}: {tbe}"
            )
    # held_issues must be filtered to actual holds. Reviewer stub above returns
    # only I1 (blocking) in followups, and doer rejects I1, so held_issues in
    # each followup event must contain exactly I1 and no stray issues.
    for fu in followups:
        held_ids = {i["id"] for i in fu["held_issues"]}
        assert held_ids <= {"I1"}, \
            f"held_issues must be filtered to actual holds, got {held_ids}"
    results["invariant_transcript"] = (r.outcome, r.rounds_used)

    # 15. INVARIANT: silently-green gates pass. `pytest -q` / `ruff check .` /
    # `mypy` on a clean tree exit 0 and print nothing; the pre-fix code
    # required non-empty stdout and misclassified these as gate failures.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass_silent, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_silent_green_gate"] = (r.outcome, r.rounds_used)

    # 15aa. INVARIANT: hanging doer at implement stage hits the doer's own
    # step timeout and escalates as ESCALATED_TIMEOUT (previously the doer
    # calls were unbounded, so this hang would wait forever).
    class HangingDoerImplement(StubDoer):
        async def implement(self, spec, acceptance):
            await asyncio.sleep(10)  # much longer than the tiny test timeout
            return "never"
    cfg = HarnessConfig()
    cfg.timeouts.model_call_seconds = 0
    cfg.escalation.retries_before_counting = 0
    cfg.escalation.consecutive_same_step_threshold = 2
    orch = make(cfg, HangingDoerImplement({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_doer_timeout"] = (r.outcome, r.rounds_used)

    # 15ab. INVARIANT: feature-wall-clock timeout emits ABORTED_BUDGET, but
    # ONLY when no finer-grained step signal fired first. The rule: "a
    # finer-grained signal is never overwritten by a coarser one." Step
    # TimeoutEscalation names WHICH step hung; ABORTED_BUDGET only says "too
    # long overall." Both firing => step wins. This test proves the outer
    # wall-clock catches a case where each individual step is fast enough
    # to not trigger step-timeout, but the SUM exceeds feature_seconds.
    class SlowButAliveDoer(StubDoer):
        async def implement(self, spec, acceptance):
            # Long enough that feature_seconds fires, short enough that the
            # step timeout (higher) does not.
            await asyncio.sleep(10)
            return "done"
    cfg = HarnessConfig()
    cfg.timeouts.model_call_seconds = 60  # step timeout NOT hit
    cfg.timeouts.feature_seconds = 0      # feature timeout WILL fire immediately
    orch = make(cfg, SlowButAliveDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_feature_budget"] = (r.outcome, r.rounds_used)

    # 15ac. INVARIANT: human_only_paths matches file paths from `diff --git`
    # headers, not substrings of the diff text. Guards three regressions:
    # (a) a code COMMENT mentioning a sensitive path must NOT trigger; the
    # old p in diff check would false-positive.
    # (b) A file renamed OUT of a sensitive tree MUST still trigger --
    # either the `a/` or `b/` side matching is enough.
    # (c) A normal change under the sensitive tree DOES trigger.
    cfg = HarnessConfig()
    cfg.human_only_paths = ["src/security/"]
    # (a) Comment-only: should NOT trigger; outcome is normal PASSED.
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_comment_only)
    r_comment = await orch.run_feature("spec", "acc")
    assert r_comment.outcome == Outcome.PASSED, \
        f"comment mentioning sensitive path must not trigger routing: got {r_comment.outcome}"
    # (b) Rename out of sensitive tree: MUST trigger human-only routing.
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_rename_out_of_sensitive)
    r_rename = await orch.run_feature("spec", "acc")
    assert r_rename.outcome == Outcome.ESCALATED_DISAGREEMENT, \
        f"rename out of sensitive tree must trigger human-only: got {r_rename.outcome}"
    # (c) Normal edit under sensitive tree: triggers.
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_security)
    r_normal = await orch.run_feature("spec", "acc")
    assert r_normal.outcome == Outcome.ESCALATED_DISAGREEMENT, \
        f"real change under sensitive tree must trigger human-only: got {r_normal.outcome}"

    # 15b. INVARIANT: real_get_diff includes untracked files. Pre-fix,
    # `git diff HEAD` skipped them entirely; a whole new file the doer added
    # was invisible to the reviewer.
    import os  # noqa: E402
    import subprocess  # noqa: E402
    import tempfile  # noqa: E402
    from harness.orchestrator import real_get_diff  # noqa: E402
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/tracked.txt", "w")).write("v1\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/tracked.txt", "w")).write("v2 TRACKED_MARKER\n")
        (open(f"{tmp}/new_file.py", "w")).write(
            "def UNTRACKED_MARKER(): pass\n")
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            diff = await real_get_diff()
        finally:
            os.chdir(prev_cwd)
    assert "TRACKED_MARKER" in diff, \
        f"tracked change missing from diff: {diff[:200]!r}"
    assert "UNTRACKED_MARKER" in diff, \
        f"untracked file contents missing from diff: {diff[:500]!r}"
    assert "new_file.py" in diff, \
        f"untracked file path missing from diff: {diff[:500]!r}"

    # 15c. INVARIANT: accepted `major` issues reach apply_fixes with the full
    # ReviewIssue (description, severity, suggested_fix), not just the ID.
    # Guards two composed changes in this tier: (a) major triggers debate,
    # (b) apply_fixes now takes list[ReviewIssue]+diff instead of list[str].
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "M1", "severity": "major",
                           "issue": "MAJOR_ISSUE_TEXT",
                           "suggested_fix": "MAJOR_FIX_TEXT"}]})
    doer = StubDoer({"M1": "accept"})
    orch = make(cfg, doer, reviewer, None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert len(doer.apply_fixes_calls) == 1, \
        f"expected 1 apply_fixes call, got {len(doer.apply_fixes_calls)}"
    call = doer.apply_fixes_calls[0]
    issue_ids = {i["id"] for i in call["issues"]}
    assert "M1" in issue_ids, \
        f"accepted major must reach apply_fixes: {call['issues']!r}"
    issue_texts = {i["issue"] for i in call["issues"]}
    assert "MAJOR_ISSUE_TEXT" in issue_texts, \
        f"apply_fixes must get full issue text, not just IDs: {call['issues']!r}"
    fix_texts = {i["suggested_fix"] for i in call["issues"]}
    assert "MAJOR_FIX_TEXT" in fix_texts, \
        f"apply_fixes must get suggested_fix text: {call['issues']!r}"
    assert "src/feature.py" in call["diff"], \
        f"apply_fixes must receive current diff: {call['diff']!r}"
    results["invariant_accepted_major_full_context"] = (
        r.outcome, r.rounds_used)

    # 15d. INVARIANT: real_get_diff omits oversized untracked files with a
    # visible marker (not silent absence). A doer that adds a 100 MB blob
    # could otherwise let it slip past review by omission.
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=tmp, check=True)
        # Small tracked file so the "no tracked changes" path is exercised
        # separately from the untracked-guard path.
        (open(f"{tmp}/README", "w")).write("hello\n")
        subprocess.run(["git", "add", "README"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True)
        # Oversized untracked file (10 KB > our 4 KB probe below).
        with open(f"{tmp}/big.txt", "wb") as fp:
            fp.write(b"A" * 10_000)
        # Untracked binary file (small enough for size, NUL in probe).
        with open(f"{tmp}/blob.bin", "wb") as fp:
            fp.write(b"header\x00binary payload\n")
        # Untracked source file that should be included as normal.
        (open(f"{tmp}/ok.py", "w")).write("def OK_MARKER(): pass\n")
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            diff = await real_get_diff(
                max_untracked_bytes=4 * 1024,
                binary_probe_bytes=1024,
            )
        finally:
            os.chdir(prev_cwd)
    assert "# skipped: big.txt (size" in diff, \
        f"oversized untracked must be omitted with size marker: {diff!r}"
    assert "# skipped: blob.bin (binary)" in diff, \
        f"binary untracked must be omitted with binary marker: {diff!r}"
    assert "OK_MARKER" in diff, \
        f"non-oversized non-binary untracked must be included: {diff!r}"

    # 15e-inject. INVARIANT: `# skipped:` markers encode filenames git-quotePath
    # style so a filename containing `\n`, `"`, or non-ASCII bytes CAN'T
    # inject fake diff content. Filter-based approaches (e.g. reject-if-newline)
    # are brittle; encoding is correct-by-construction.
    from harness.orchestrator import _git_quote_path  # noqa: E402
    # Newline: must be encoded as \n and the whole path quoted.
    encoded = _git_quote_path("evil\nname.py")
    assert "\n" not in encoded, \
        f"encoded path must not contain literal newline: {encoded!r}"
    assert encoded == r'"evil\nname.py"', \
        f"encoded path must be git-quoted: {encoded!r}"
    # Plain ASCII no-special-chars: pass-through.
    assert _git_quote_path("normal/path.py") == "normal/path.py"
    # Non-ASCII UTF-8: octal-encoded per git convention.
    encoded_utf8 = _git_quote_path("café.py")
    assert encoded_utf8 == r'"caf\303\251.py"', \
        f"non-ASCII path must be octal-encoded: {encoded_utf8!r}"

    # 15e. INVARIANT: `_untracked_skip_reason` short-circuits on non-regular
    # paths (FIFO, socket, device, symlink) instead of calling open() on
    # them. Regression guard for the hang scenario: pre-fix, `open(fifo,
    # "rb")` would block indefinitely, and because that's synchronous
    # inside the async coroutine, wait_for(feature_seconds) could not
    # preempt it -- the whole feature would hang past every timeout.
    # Tested directly on the helper because git's ls-files skips FIFOs
    # itself (defense-in-depth: if any other caller ever hands us one, the
    # guard must fire without the hang).
    from harness.orchestrator import _untracked_skip_reason  # noqa: E402
    with tempfile.TemporaryDirectory() as tmp:
        fifo_path = f"{tmp}/trap"
        os.mkfifo(fifo_path)
        reason = _untracked_skip_reason(fifo_path, 1_000_000, 8192)
        assert reason == "non-regular", \
            f"FIFO must produce 'non-regular' skip reason, got {reason!r}"
        # Symlink case too -- following symlinks blindly is a similar
        # class of risk (dangling symlink -> stat error, symlink loop ->
        # untold pain). Explicit "symlink" marker so the reviewer sees why
        # it wasn't included.
        target = f"{tmp}/target.txt"
        (open(target, "w")).write("hello\n")
        link_path = f"{tmp}/link"
        os.symlink(target, link_path)
        reason = _untracked_skip_reason(link_path, 1_000_000, 8192)
        assert reason == "symlink", \
            f"symlink must produce 'symlink' skip reason, got {reason!r}"
        # A vanished path (referenced but doesn't exist) must surface as
        # "unreadable", not raise.
        reason = _untracked_skip_reason(
            f"{tmp}/does-not-exist", 1_000_000, 8192)
        assert reason == "unreadable", \
            f"missing path must produce 'unreadable' skip reason, got {reason!r}"

    # 15f. INVARIANT: `human_only_paths` matching handles git-quoted paths.
    # A sensitive filename with special characters would previously slip
    # past routing because the regex-based header parser didn't unquote.
    from harness.orchestrator import _extract_changed_paths  # noqa: E402
    # Quoted header from a rename of "src/security/a b.py" (space in name).
    quoted_diff = (
        'diff --git "a/src/security/a b.py" "b/src/security/a b.py"\n'
        '@@ -1 +1 @@\n-old\n+new\n'
    )
    paths = _extract_changed_paths(quoted_diff)
    assert "src/security/a b.py" in paths, \
        f"quoted path must be extracted with space intact: {paths!r}"
    # Escaped-quote inside a filename, non-ASCII via octal.
    tricky_diff = 'diff --git "a/x\\"y.py" "b/f\\303\\251\\.py"\n@@ -1 +1 @@\n-a\n+b\n'
    paths = _extract_changed_paths(tricky_diff)
    assert 'x"y.py' in paths, \
        f"escaped-quote path must decode: {paths!r}"
    assert "fé\\.py" in paths, \
        f"octal-encoded non-ASCII path must decode: {paths!r}"

    # 15f-cc. INVARIANT: merge/combined diff headers (`diff --cc`,
    # `diff --combined`) trigger human_only_paths routing. Pre-fix, only
    # `diff --git` headers were parsed; a merge conflict touching a
    # sensitive file would silently be sent to the external reviewer.
    cc_paths = _extract_changed_paths(
        'diff --cc src/security/merged.py\n@@@ @@@\n-a\n+b\n')
    assert cc_paths == ["src/security/merged.py"], \
        f"diff --cc header must extract path: {cc_paths!r}"
    combined_paths = _extract_changed_paths(
        'diff --combined src/security/x.py\n@@@ @@@\n')
    assert combined_paths == ["src/security/x.py"], \
        f"diff --combined header must extract path: {combined_paths!r}"
    # Quoted merged path.
    quoted_cc_paths = _extract_changed_paths(
        'diff --cc "src/security/a b.py"\n@@@ @@@\n')
    assert quoted_cc_paths == ["src/security/a b.py"], \
        f"quoted diff --cc must decode: {quoted_cc_paths!r}"

    # 15f-prefix. INVARIANT: human_only_paths prefix match respects path
    # boundaries. `["src/security"]` must NOT match `src/security_bypass.py`.
    async def diff_bypass_lookalike():
        return ('diff --git a/src/security_bypass.py b/src/security_bypass.py\n'
                '@@ -1 +1 @@\n-a\n+b\n')
    cfg = HarnessConfig()
    cfg.human_only_paths = ["src/security"]  # NO trailing slash
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_bypass_lookalike)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.PASSED, \
        f"src/security_bypass.py must not match human_only_paths=['src/security']: {r.outcome}"

    # 15g. INVARIANT: end-to-end path-aware routing still triggers on
    # quoted paths. A rename involving a sensitive-tree filename with a
    # space must route to human-only.
    async def diff_quoted_sensitive():
        return (
            'diff --git "a/src/security/a b.py" "b/src/security/a b.py"\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
    cfg = HarnessConfig()
    cfg.human_only_paths = ["src/security/"]
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                None, gate_pass, diff_quoted_sensitive)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_DISAGREEMENT, \
        f"quoted sensitive path must trigger human-only: {r.outcome}"

    # 15h. INVARIANT: malformed doer JSON escalates cleanly as
    # ESCALATED_NO_SIGNAL, not an uncaught Pydantic ValidationError. The
    # doer's `respond_to_review` return is validated OUTSIDE StepRunner's
    # error boundary, so ValidationError previously propagated to run_feature
    # which doesn't know about pydantic -> uncaught traceback.
    class MalformedDoer(StubDoer):
        async def respond_to_review(self, spec, acceptance, diff, verdict):
            class FakeResp:
                def model_dump_json(self):
                    return "this is not valid json"
            return FakeResp()
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": []}])
    orch = make(cfg, MalformedDoer({"I1": "reject"}), reviewer, None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_doer_malformed_response"] = (r.outcome, r.rounds_used)

    # 15h-real. INVARIANT: the REAL RealDoerClient.respond_to_review parses
    # live model output INSIDE StepRunner's coroutine. A prose reply (no JSON)
    # or a schema mismatch must surface as RuntimeError -- the same "this call
    # failed" contract run_subprocess uses for CLI failures -- so StepRunner's
    # narrow (RuntimeError, OSError) catch turns it into a non-ok StepResult
    # -> ModelUnavailable -> ESCALATED_NO_SIGNAL, NOT an uncaught ValueError /
    # pydantic ValidationError that crashes the orchestrator mid-debate. We
    # drive the real subprocess path (not a stub) via a tiny executable helper
    # pointed at by claude_cmd; the appended `-p` flag is ignored by the helper.
    import os  # noqa: E402
    import stat  # noqa: E402
    import sys  # noqa: E402
    import tempfile  # noqa: E402
    from pydantic import ValidationError  # noqa: E402
    from harness.orchestrator import RealDoerClient  # noqa: E402
    from harness.schemas import ReviewVerdict, ReviewIssue  # noqa: E402

    verdict = ReviewVerdict(issues=[
        ReviewIssue(id="I1", severity="blocking",
                    issue="something is wrong", suggested_fix="fix it")])

    def _write_helper(tmpdir, body):
        # Executable python helper: ignores its args, prints `body` to stdout.
        path = f"{tmpdir}/fake_claude.py"
        with open(path, "w") as fp:
            fp.write(f"#!{sys.executable}\n")
            fp.write("import sys\n")
            fp.write(f"sys.stdout.write({body!r})\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)
        return path

    # (a) Prose reply -> RuntimeError mentioning "malformed", never ValueError
    # or pydantic ValidationError.
    with tempfile.TemporaryDirectory() as tmp:
        helper = _write_helper(tmp, "I think these issues all look valid to me.")
        doer = RealDoerClient(claude_cmd=helper)
        raised = None
        try:
            await doer.respond_to_review("spec", "acc", "diff", verdict)
        except BaseException as e:  # capture the exact type that escapes
            raised = e
        assert isinstance(raised, RuntimeError), \
            f"prose reply must raise RuntimeError, got {type(raised).__name__}: {raised!r}"
        assert not isinstance(raised, (ValueError, ValidationError)), \
            f"must not leak ValueError/ValidationError: {type(raised).__name__}"
        assert "malformed" in str(raised), \
            f"RuntimeError message must mention 'malformed': {raised!r}"

    # (b) Happy path: valid response JSON still parses to a DoerResponse with
    # the expected decision -- the wrapper does not break normal parsing.
    with tempfile.TemporaryDirectory() as tmp:
        valid = json.dumps({"responses": [
            {"id": "I1", "decision": "reject",
             "reasoning": "the reviewer misread the diff"}]})
        helper = _write_helper(tmp, valid)
        doer = RealDoerClient(claude_cmd=helper)
        resp = await doer.respond_to_review("spec", "acc", "diff", verdict)
        assert isinstance(resp, DoerResponse), \
            f"valid JSON must parse to DoerResponse, got {type(resp).__name__}"
        assert resp.rejected_ids() == {"I1"}, \
            f"expected I1 rejected, got responses={resp.responses!r}"

    # 15i. INVARIANT: HarnessConfig.validate() rejects feature_seconds smaller
    # than the largest step timeout. The "finer signal never overwritten"
    # guarantee only holds when steps can raise TimeoutEscalation before the
    # outer wall-clock cancels the task; making the violating config
    # unrepresentable at load time is stronger than handling the runtime race.
    from harness.config import HarnessConfig as _HC  # noqa: E402
    bad = _HC()
    bad.timeouts.feature_seconds = 30
    bad.timeouts.model_call_seconds = 120
    try:
        bad.validate()
        assert False, "validate() must reject feature_seconds < max step timeout"
    except ValueError:
        pass  # expected
    # Zero feature_seconds is the deliberate "fire immediately" test case;
    # validate() must NOT reject it (used by invariant_feature_budget above).
    ok = _HC()
    ok.timeouts.feature_seconds = 0
    ok.validate()  # must not raise
    # Negative timeouts rejected.
    neg = _HC()
    neg.timeouts.model_call_seconds = -1
    try:
        neg.validate()
        assert False, "validate() must reject negative timeouts"
    except ValueError:
        pass

    # 15j. INVARIANT: DiffConfig knobs flow to behavior THROUGH the production
    # callable. Pre-fix, cli passed bare `real_get_diff` and the Orchestrator
    # invoked it zero-arg, so `cfg.diff.max_untracked_bytes` /
    # `binary_probe_bytes` were dead. `bound_get_diff(cfg.diff)` is the exact
    # object production wires, so exercising it proves config reaches behavior.
    from harness.orchestrator import bound_get_diff  # noqa: E402
    from harness.config import DiffConfig  # noqa: E402

    def _init_repo(tmp):
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/README", "w")).write("hello\n")
        subprocess.run(["git", "add", "README"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True)

    async def _diff_via(tmp, get_diff):
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            return await get_diff()
        finally:
            os.chdir(prev_cwd)

    # (a) Restrictive max_untracked_bytes skips; permissive default includes --
    #     both through bound_get_diff, the production call shape.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        (open(f"{tmp}/notes.txt", "w")).write("X" * 64 + "\n")  # > 16 bytes
        restrictive = await _diff_via(
            tmp, bound_get_diff(DiffConfig(max_untracked_bytes=16)))
        permissive = await _diff_via(tmp, bound_get_diff(DiffConfig()))
    assert "# skipped: notes.txt (size" in restrictive, \
        f"tiny max_untracked_bytes must skip via factory: {restrictive!r}"
    assert "# skipped: notes.txt (size" not in permissive, \
        f"permissive default must NOT skip: {permissive!r}"
    assert "XXXX" in permissive, \
        f"permissive default must include file content: {permissive!r}"

    # (b) binary_probe_bytes flows both directions. First NUL sits at offset
    #     100: a probe smaller than that finds no NUL (treated as text, not
    #     skipped); a probe larger than that finds it (skipped as binary).
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        with open(f"{tmp}/probe.bin", "wb") as fp:
            fp.write(b"T" * 100 + b"\x00" + b"tail\n")
        small_probe = await _diff_via(
            tmp, bound_get_diff(DiffConfig(binary_probe_bytes=16)))
        large_probe = await _diff_via(
            tmp, bound_get_diff(DiffConfig(binary_probe_bytes=1024)))
    assert "# skipped: probe.bin (binary)" not in small_probe, \
        f"probe smaller than NUL offset must NOT skip as binary: {small_probe!r}"
    assert "probe.bin" in small_probe, \
        f"non-skipped file must appear in diff: {small_probe!r}"
    assert "# skipped: probe.bin (binary)" in large_probe, \
        f"probe larger than NUL offset must skip as binary: {large_probe!r}"

    # 15k. INVARIANT: deleting the dead round_seconds field drops the phantom
    # validation floor. feature_seconds=350 >= max(model_call=120, gate=300)
    # now validates; under the old code the dead default round_seconds=400
    # made max(step_bounds)=400 > 350 and raised for a knob that governed
    # nothing.
    floor = _HC()
    floor.timeouts.model_call_seconds = 120
    floor.timeouts.gate_seconds = 300
    floor.timeouts.feature_seconds = 350
    floor.validate()  # must not raise

    # 15l. INVARIANT: round_seconds stays deleted from Timeouts. Trivially true
    # today; guards against the field being reintroduced without being wired to
    # a real per-round budget (dead config that constrains live config).
    from harness.config import Timeouts as _TO  # noqa: E402
    assert not hasattr(_TO(), "round_seconds"), \
        "round_seconds must not be reintroduced without wiring"

    # 15m. INVARIANT: old TOML setting timeouts.round_seconds still loads. The
    # `_apply` hasattr guard silently ignores it (same contract as any unknown
    # key) -- neither raises nor creates the attribute.
    stale = _HC()
    stale._apply({"timeouts": {"round_seconds": 999}})  # must not raise
    assert not hasattr(stale.timeouts, "round_seconds"), \
        "stale round_seconds key must NOT create the attribute"

    # 15n. INVARIANT: the dead `interactive` knob stays deleted (config-drift 2b).
    # Nothing in harness/ ever read it; guards against reintroduction without a
    # reader, same rationale as round_seconds above.
    assert not hasattr(_HC(), "interactive"), \
        "interactive must not be reintroduced without a reader"

    # 15o. INVARIANT: old profile TOML setting `interactive` still loads. The
    # `_apply` top-level tuple no longer lists it, so the key is silently ignored
    # (same contract as any unknown key) -- neither raises nor creates the
    # attribute -- while a sibling live key like `profile` still applies.
    stale_i = _HC()
    stale_i._apply({"interactive": False, "profile": "ci"})  # must not raise
    assert not hasattr(stale_i, "interactive"), \
        "stale interactive key must NOT create the attribute"
    assert stale_i.profile == "ci", \
        "live sibling key `profile` must still apply alongside a dead one"

    # 15p. INVARIANT: HARNESS_NONINTERACTIVE is a no-op. Setting it must not
    # raise and must not create an `interactive` attribute (the env hook was
    # deleted with the field, not kept as a dead compatibility shim).
    _prev_ni = os.environ.get("HARNESS_NONINTERACTIVE")
    os.environ["HARNESS_NONINTERACTIVE"] = "1"
    try:
        env_i = _HC()
        env_i._apply_env()  # must not raise
        assert not hasattr(env_i, "interactive"), \
            "HARNESS_NONINTERACTIVE must not create an interactive attribute"
    finally:
        if _prev_ni is None:
            os.environ.pop("HARNESS_NONINTERACTIVE", None)
        else:
            os.environ["HARNESS_NONINTERACTIVE"] = _prev_ni

    # 15q. INVARIANT: the live env sibling HARNESS_NO_DEBATE still flips
    # debate_enabled (guards against over-deleting in `_apply_env`).
    _prev_nd = os.environ.get("HARNESS_NO_DEBATE")
    os.environ["HARNESS_NO_DEBATE"] = "1"
    try:
        env_d = _HC()
        env_d._apply_env()
        assert env_d.debate_enabled is False, \
            "HARNESS_NO_DEBATE=1 must still flip debate_enabled to False"
    finally:
        if _prev_nd is None:
            os.environ.pop("HARNESS_NO_DEBATE", None)
        else:
            os.environ["HARNESS_NO_DEBATE"] = _prev_nd

    # 16. INVARIANT: rejected `major` issues reach the deadlock/tiebreak path
    # instead of being silently discarded. Pre-fix, `unresolved_blocking` was
    # computed from `blocking_ids` only, so a rejected major left it empty and
    # the loop exited PASSED with the concern unresolved.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "major",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": [{"id": "I1", "severity": "major",
                                "issue": "x", "suggested_fix": "y"}]}] * 3)
    # No tiebreaker -> any unresolved deadlock escalates directly.
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_major_deadlocks"] = (r.outcome, r.rounds_used)

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

    # 15. INVARIANT: a bare top-level empty array parses as an empty verdict.
    # `[]` is the happy-path shape of a clean followup review ("no remaining
    # issues held"). Before the fix, _extract_json found no {...} window and
    # raised, crashing run_feature preferentially when the reviewer was
    # SATISFIED. Direct-call assertion: no exception, issues == [].
    verdict = _parse_verdict("[]")
    assert verdict.issues == [], \
        f"bare [] must parse to empty issues, got {verdict.issues!r}"
    results["invariant_bare_empty_array_verdict_parses"] = (Outcome.PASSED, 0)

    # 16. INVARIANT: a bare top-level populated array parses with full schema
    # validation -- coerced to {"issues": [...]} at the parse boundary.
    verdict = _parse_verdict(
        '[{"id": "I1", "severity": "blocking", '
        '"issue": "x", "suggested_fix": "y"}]')
    assert len(verdict.issues) == 1, \
        f"expected 1 issue, got {len(verdict.issues)}"
    assert verdict.issues[0].id == "I1"
    assert verdict.issues[0].severity == "blocking"
    assert verdict.issues[0].issue == "x"
    assert verdict.issues[0].suggested_fix == "y"
    results["invariant_bare_populated_array_verdict_parses"] = (Outcome.PASSED, 1)

    # 17. INVARIANT: a followup reviewer that returns literal "[]" ends the
    # debate cleanly with PASSED and does NOT crash. End-to-end regression
    # against the menu traceability-slice crash: reviewer holds one blocking
    # issue, doer rejects, followup returns bare "[]" -> loop converges.
    cfg = HarnessConfig()
    reviewer = RawFollowupReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=["[]"])  # reviewer drops it via bare top-level array
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_followup_bare_array_ends_debate_cleanly"] = (
        r.outcome, r.rounds_used)

    # 18. INVARIANT (drift-11a): an empty DoerResponse to a non-empty verdict
    # escalates as a protocol violation -- it never converges silently to
    # PASSED. The doer took no stance on the reviewer's blocking issue, which is
    # no signal, the same bucket as a timeout or an unavailable model.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]})
    orch = make(cfg, EmptyResponseDoer({}), reviewer, None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"empty doer response must escalate no-signal, got {r.outcome}"
    assert r.escalation_reason and "protocol violation" in r.escalation_reason, \
        f"escalation_reason must name the protocol violation: {r.escalation_reason!r}"
    assert "I1" in r.escalation_reason, \
        f"escalation_reason must name the missing id I1: {r.escalation_reason!r}"
    # The violation event is logged BEFORE the escalation event, with the
    # documented v1 shape.
    events = [e["event"] for e in r.debate_log]
    assert "doer_protocol_violation" in events, \
        f"expected doer_protocol_violation event in log: {events}"
    dpv = next(e for e in r.debate_log if e["event"] == "doer_protocol_violation")
    assert dpv["event_version"] == 1
    assert dpv["missing_ids"] == ["I1"], f"missing_ids: {dpv['missing_ids']}"
    assert dpv["verdict_issue_count"] == 1
    assert dpv["responded_id_count"] == 0
    # The violation is logged in _run BEFORE the exception unwinds; the
    # run_feature handler returns without logging anything further, so the
    # violation event is the last thing in the transcript.
    assert events[-1] == "doer_protocol_violation", \
        f"doer_protocol_violation must be the last logged event: {events}"
    results["invariant_empty_doer_response_escalates"] = (
        r.outcome, r.rounds_used)

    # 19. INVARIANT (drift-11a): a partial DoerResponse (strict subset of the
    # verdict ids) escalates the same way; the escalation reason names the
    # omitted subset, not the responded id.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"},
                          {"id": "I2", "severity": "blocking",
                           "issue": "x2", "suggested_fix": "y2"}]})
    orch = make(cfg, PartialResponseDoer({}), reviewer, None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"partial doer response must escalate, got {r.outcome}"
    assert r.escalation_reason and "I2" in r.escalation_reason, \
        f"escalation_reason must name the missing id I2: {r.escalation_reason!r}"
    assert "I1" not in r.escalation_reason, \
        f"escalation_reason must not name the responded id I1: {r.escalation_reason!r}"
    dpv = next(e for e in r.debate_log if e["event"] == "doer_protocol_violation")
    assert dpv["missing_ids"] == ["I2"], f"missing_ids: {dpv['missing_ids']}"
    assert dpv["verdict_issue_count"] == 2
    assert dpv["responded_id_count"] == 1
    results["invariant_partial_doer_response_escalates"] = (
        r.outcome, r.rounds_used)

    # 20. INVARIANT (drift-11a): a conformant DoerResponse (stance on every
    # verdict issue) does NOT trigger the violation -- normal debate flow. This
    # is the convergence path from case 2, with an explicit assertion that no
    # doer_protocol_violation event is emitted.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": []}])  # reviewer concedes
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer, None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.PASSED, \
        f"conformant response must pass, got {r.outcome}"
    assert not any(e["event"] == "doer_protocol_violation"
                   for e in r.debate_log), \
        "conformant response must not emit a protocol violation event"
    results["invariant_conformant_doer_response_still_passes"] = (
        r.outcome, r.rounds_used)

    # 21. UNIT: DoerProtocolViolation carries the missing ids and round for the
    # run_feature handler to build the escalation reason from.
    dpv_exc = DoerProtocolViolation({"I2", "I3"}, 2)
    assert dpv_exc.missing_ids == {"I2", "I3"}
    assert dpv_exc.round == 2

    # 22. INVARIANT: an errored gate CALLABLE is no-signal, NOT a gate failure.
    # run_gate raising (verify.sh missing, OSError, non-zero run_gate) produced
    # NO verdict about the code, so the outcome must be ESCALATED_NO_SIGNAL with
    # "[gate]" in the reason -- never ESCALATED_GATE ("the code failed the
    # gate"). Covers the initial-gate site.
    cfg = HarnessConfig()
    async def gate_raises():
        raise RuntimeError("verify.sh: not found")
    orch = Orchestrator(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                        None, gate_raises, diff_plain,
                        ab_swap=lambda _id: False)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"errored gate must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[gate]" in r.escalation_reason, \
        f"escalation_reason must be tagged [gate]: {r.escalation_reason!r}"
    results["gate_error_no_signal"] = (r.outcome, r.rounds_used)

    # 23. INVARIANT: a malformed GateResult (gate returns a non-JSON string,
    # e.g. a stale caller on the old str contract) is the SAME no-signal
    # bucket, not a gate failure.
    cfg = HarnessConfig()
    async def gate_malformed():
        return "this is not json"
    orch = Orchestrator(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                        None, gate_malformed, diff_plain,
                        ab_swap=lambda _id: False)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"malformed gate must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[gate]" in r.escalation_reason, \
        f"escalation_reason must be tagged [gate]: {r.escalation_reason!r}"
    results["gate_malformed_no_signal"] = (r.outcome, r.rounds_used)

    # 24. INVARIANT (guards against over-rotating #22/#23): a gate that RAN and
    # reported ok=False is still a real gate failure -> ESCALATED_GATE. This is
    # case 6 above, re-asserted here alongside the no-signal cases to keep the
    # ran-and-failed vs never-ran distinction pinned.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_fail, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_GATE, \
        f"a gate that ran and failed must stay ESCALATED_GATE, got {r.outcome}"
    results["gate_ran_and_failed"] = (r.outcome, r.rounds_used)

    # 25. INVARIANT: zero accepted issues after debate skips the apply_fixes
    # model call. Reviewer raises one blocking issue, doer rejects with
    # reasoning, reviewer follow-up concedes (nothing held) -> accepted set is
    # empty. Step 7's doer call must be SKIPPED (the pointless no-op that could
    # itself error and escalate a converged run), an apply_fixes_skipped event
    # logged, and step 8 (post-fix gate) STILL run -> PASSED.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": []}])  # reviewer concedes, nothing held
    doer = StubDoer({"I1": "reject"})
    gate_calls = []
    async def gate_pass_recording():
        gate_calls.append(True)
        return (True, "green")
    orch = make(cfg, doer, reviewer, None, gate_pass_recording, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.PASSED, \
        f"converged empty-accept must PASS, got {r.outcome}"
    assert doer.apply_fixes_calls == [], \
        f"apply_fixes must not run with zero accepted issues: {doer.apply_fixes_calls}"
    events = [e["event"] for e in r.debate_log]
    assert "apply_fixes_skipped" in events, \
        f"expected apply_fixes_skipped event in log: {events}"
    skipped = next(e for e in r.debate_log if e["event"] == "apply_fixes_skipped")
    assert skipped["reason"] == "no_accepted_issues", \
        f"apply_fixes_skipped must record its reason: {skipped}"
    # Post-fix gate STILL ran: initial gate + post-fix gate == 2 invocations.
    assert len(gate_calls) == 2, \
        f"post-fix gate must still run when apply is skipped, saw {len(gate_calls)} gate calls"
    results["empty_accept_skips_apply"] = (r.outcome, r.rounds_used)

    # 26. INVARIANT: a reviewer that answers with prose (no JSON) on the initial
    # review escalates cleanly as ESCALATED_NO_SIGNAL. The parse raises
    # ValueError OUTSIDE StepRunner's error boundary, so it previously crashed
    # run_feature with an uncaught traceback -- same class as the doer path.
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), ProseReviewer(cfg), None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_reviewer_prose_escalates"] = (r.outcome, r.rounds_used)

    # 27. INVARIANT: a tiebreaker that answers with malformed output on a
    # deadlocked issue escalates the WHOLE run as ESCALATED_NO_SIGNAL -- matching
    # the errored-tiebreaker branch, NOT the timeout branch (which merely leaves
    # the issue contested). The parse would otherwise raise ValueError inside the
    # per-issue loop and crash run_feature.
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg,
        first={"issues": [{"id": "I1", "severity": "blocking",
                           "issue": "x", "suggested_fix": "y"}]},
        followups=[{"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]}] * 3)
    orch = make(cfg, StubDoer({"I1": "reject"}), reviewer,
                MalformedTiebreaker(cfg), gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    results["invariant_tiebreaker_malformed_escalates"] = (
        r.outcome, r.rounds_used)

    # 28. INVARIANT: _extract_verdict_json tolerates the three array shapes.
    # (a) bare empty array.
    assert _extract_verdict_json("[]") == {"issues": []}, "bare [] must parse"
    # (b) array followed by prose whose text contains `]` -- rfind lands on the
    # `]` in "[the docs]", the widest slice is unparseable, and the array path
    # must walk `]` candidates leftward until the real array parses.
    trailing = "[]\n\nNote: see [the docs] for details."
    assert _extract_verdict_json(trailing) == {"issues": []}, \
        f"array + prose containing ] must parse, got {_extract_verdict_json(trailing)!r}"
    # (b') POPULATED one-issue array followed by prose whose text contains `]`.
    # The `[]`-only case above can't catch a regression that mishandles a
    # non-empty array's rightmost real `]`: here the widest slice spans the
    # trailing "[link]" and is unparseable, so the leftward `]` walk must land
    # on the array's own closing bracket and recover the single issue.
    populated_trailing = (
        '[{"id": "I1", "severity": "blocking", "issue": "x", '
        '"suggested_fix": "y"}]\n\nNote: see [the link] above.')
    got_pt = _extract_verdict_json(populated_trailing)
    assert [i["id"] for i in got_pt["issues"]] == ["I1"], \
        f"populated array + prose containing ] must parse to the issue, got {got_pt!r}"
    assert got_pt["issues"][0]["issue"] == "x", \
        f"populated array + prose must preserve issue fields, got {got_pt!r}"
    # (c) fenced ```json array.
    fenced = ('```json\n'
              '[{"id": "I1", "severity": "blocking", '
              '"issue": "x", "suggested_fix": "y"}]\n'
              '```')
    got = _extract_verdict_json(fenced)
    assert [i["id"] for i in got["issues"]] == ["I1"], \
        f"fenced array must parse to one issue, got {got!r}"
    results["invariant_verdict_array_shapes_parse"] = (Outcome.PASSED, 0)

    # 29. INVARIANT (Member A): the parse-boundary catches are narrowed to
    # ValueError, so a NON-ValueError from a parse path (a harness bug -- e.g.
    # a TypeError from a refactored _extract_json) PROPAGATES and crashes the
    # run rather than being mislabelled "malformed response" and buried as
    # no-signal. We monkeypatch _extract_json to raise TypeError; the reviewer
    # returns valid JSON so the parse actually reaches the extractor. The
    # TypeError must escape run_feature entirely (its handlers cover
    # Timeout/ModelUnavailable/DoerProtocolViolation only, never TypeError).
    import harness.orchestrator as _orch_mod  # noqa: E402
    _real_extract = _orch_mod._extract_json

    def _boom_extract(text):
        raise TypeError("simulated harness refactor bug")

    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain)
    _orch_mod._extract_json = _boom_extract
    try:
        crashed = None
        try:
            await orch.run_feature("spec", "acc")
        except BaseException as e:  # capture exactly what escapes
            crashed = e
        assert isinstance(crashed, TypeError), (
            "a TypeError from a parse path must propagate (narrow ValueError "
            f"catch), got {type(crashed).__name__ if crashed else None}: "
            f"{crashed!r}")
    finally:
        _orch_mod._extract_json = _real_extract

    # 30. INVARIANT (Member B): get_diff runs under StepRunner, so a get_diff
    # callable that ERRORS is no-signal -> ESCALATED_NO_SIGNAL tagged
    # "[get_diff]", the same contract as an errored gate/reviewer. Bare
    # `await self.get_diff()` would have let the RuntimeError crash the run.
    cfg = HarnessConfig()
    async def diff_raises():
        raise RuntimeError("git index.lock held")
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_raises)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"erroring get_diff must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[get_diff]" in r.escalation_reason, \
        f"escalation_reason must be tagged [get_diff]: {r.escalation_reason!r}"

    # 31. INVARIANT (Member B): a HANGING get_diff fires the step-level timeout
    # under the gate_seconds bound and escalates ESCALATED_TIMEOUT -- BEFORE the
    # coarse feature budget -- proving the finer step signal wins. The gate
    # passes instantly; only get_diff hangs. gate_seconds=1 bounds get_diff;
    # feature_seconds stays large so ABORTED_BUDGET can't fire first.
    cfg = HarnessConfig()
    cfg.timeouts.gate_seconds = 1        # bounds get_diff (local tooling)
    cfg.timeouts.model_call_seconds = 1  # keep max_step small for validate()
    cfg.timeouts.feature_seconds = 120   # >> the ~1s get_diff hang
    cfg.escalation.retries_before_counting = 0  # single attempt -> fast
    async def diff_hang():
        await asyncio.sleep(30)
        return "never"
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_hang)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_TIMEOUT, \
        f"hanging get_diff must escalate TIMEOUT (not budget), got {r.outcome}"
    assert r.escalation_reason and "get_diff_no_signal" in r.escalation_reason, \
        f"escalation_reason must name the get_diff step: {r.escalation_reason!r}"

    # 32. INVARIANT (Member D2a): RealDoerClient.respond_to_review fed valid
    # JSON that FAILS schema validation (missing required IssueResponse fields)
    # must raise RuntimeError("malformed"), NOT leak a pydantic ValidationError.
    # Prior malformed tests only fed prose; this pins the valid-JSON-wrong-schema
    # branch through the real subprocess path via the fake-CLI helper.
    verdict_wrongschema = ReviewVerdict(issues=[
        ReviewIssue(id="I1", severity="blocking",
                    issue="something is wrong", suggested_fix="fix it")])
    with tempfile.TemporaryDirectory() as tmp:
        # Valid JSON, but each response is missing `decision` and `reasoning`.
        helper = _write_helper(tmp, json.dumps({"responses": [{"id": "I1"}]}))
        doer = RealDoerClient(claude_cmd=helper)
        raised = None
        try:
            await doer.respond_to_review("spec", "acc", "diff",
                                         verdict_wrongschema)
        except BaseException as e:
            raised = e
        assert isinstance(raised, RuntimeError), \
            f"wrong-schema JSON must raise RuntimeError, got {type(raised).__name__}: {raised!r}"
        assert not isinstance(raised, (ValueError, ValidationError)), \
            f"must not leak ValueError/ValidationError: {type(raised).__name__}"
        assert "malformed" in str(raised), \
            f"RuntimeError message must mention 'malformed': {raised!r}"

    # 33. INVARIANT (Member D2b): a stub reviewer returning valid JSON that
    # fails ReviewVerdict validation escalates ESCALATED_NO_SIGNAL, same bucket
    # as prose. (A bare wrong key like {"wrong_key": []} is ACCEPTED -- `issues`
    # defaults to [] -- so it can't exercise the failure path; we feed valid
    # JSON whose issues entry violates the schema to actually trip validation.)
    cfg = HarnessConfig()
    reviewer = StubReviewer(cfg, {"issues": [{"id": "I1"}]})  # missing fields
    orch = make(cfg, StubDoer({}), reviewer, None, gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"wrong-schema reviewer JSON must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[reviewer]" in r.escalation_reason, \
        f"escalation_reason must be tagged [reviewer]: {r.escalation_reason!r}"

    # 34. INVARIANT (Member D4): cmd_fix's Orchestrator construction is factored
    # into build_orchestrator(cfg), and the production get_diff callable carries
    # its bound DiffConfig introspectably. Reverting cli to pass bare
    # real_get_diff (defaults shadowing the config) would fail this: the wired
    # callable's diff_cfg must be the very cfg.diff object.
    from harness.cli import build_orchestrator, ensure_clean_tree  # noqa: E402
    from harness.orchestrator import (  # noqa: E402
        real_restore_tree, _noop_restore_tree)
    cfg = HarnessConfig()
    built = build_orchestrator(cfg)
    assert built.get_diff.diff_cfg is cfg.diff, \
        "build_orchestrator must wire the config-bound get_diff (diff_cfg is cfg.diff)"
    # allow_dirty=False wires the REAL git restore; True wires the no-op (we
    # can't safely reset a tree whose baseline we don't own).
    assert built.restore_tree is real_restore_tree, \
        "build_orchestrator(allow_dirty=False) must wire real_restore_tree"
    built_dirty = build_orchestrator(cfg, allow_dirty=True)
    assert built_dirty.restore_tree is _noop_restore_tree, \
        "build_orchestrator(allow_dirty=True) must wire the no-op restore"
    # --allow-dirty leaves a visible marker in the transcript.
    assert any(e["event"] == "tree_hygiene_disabled"
               for e in built_dirty.log), \
        "build_orchestrator(allow_dirty=True) must log tree_hygiene_disabled"

    # 35. INVARIANT (tree-hygiene): every implement attempt is preceded by a
    # restore, and retries start clean. A doer whose implement always times out
    # (positive step timeout + retry) drives two attempts; the recording
    # restore must fire BEFORE each attempt, interleaved
    # (restore, attempt, restore, attempt).
    class TimingOutImplementDoer(StubDoer):
        def __init__(self, order):
            super().__init__({})
            self.order = order
        async def implement(self, spec, acceptance):
            self.order.append("attempt")
            await asyncio.sleep(30)  # exceeds the positive step timeout below
            return "never"
    cfg = HarnessConfig()
    cfg.timeouts.model_call_seconds = 1   # positive: factory actually runs
    cfg.escalation.retries_before_counting = 1  # -> 2 attempts (retry once)
    cfg.escalation.consecutive_same_step_threshold = 5  # don't escalate early
    cfg.escalation.timeout_count_threshold = 5
    order: list = []
    restore = RecordingRestore(order)
    doer = TimingOutImplementDoer(order)
    orch = make(cfg, doer, StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert restore.calls >= 2, \
        f"restore must run before both attempts, saw {restore.calls} calls"
    assert order[:4] == ["restore", "attempt", "restore", "attempt"], \
        f"restore must interleave before each attempt, got {order!r}"
    # A tree_restored event records the discarded partial work on the timeout.
    tr = [e for e in r.debate_log if e["event"] == "tree_restored"]
    assert tr and tr[0]["reason"] == "implement_timeout", \
        f"implement timeout must log tree_restored(implement_timeout): {tr!r}"
    assert r.outcome == Outcome.ESCALATED_TIMEOUT, \
        f"repeated implement timeout must escalate TIMEOUT, got {r.outcome}"

    # 36. INVARIANT (tree-hygiene): an implement ERROR restores the tree and
    # logs tree_restored(implement_error); outcome is ESCALATED_NO_SIGNAL. The
    # error is not retried, so the restore runs (once per-attempt, once on the
    # failure branch) before the escalation propagates.
    class ErroringImplementDoer(StubDoer):
        async def implement(self, spec, acceptance):
            raise RuntimeError("claude exited 1: implement crashed")
    cfg = HarnessConfig()
    restore = RecordingRestore()
    orch = make(cfg, ErroringImplementDoer({}),
                StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert restore.calls >= 1, \
        f"implement error must restore the tree, saw {restore.calls} calls"
    tr = [e for e in r.debate_log if e["event"] == "tree_restored"]
    assert tr and tr[0]["reason"] == "implement_error", \
        f"implement error must log tree_restored(implement_error): {tr!r}"
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"implement error must escalate NO_SIGNAL, got {r.outcome}"

    # 37. INVARIANT (tree-hygiene): the tree is NEVER restored after implement
    # has succeeded. Both a full PASSED run and a post-implement escalation
    # (gate failure) must show restore_tree call count == number of implement
    # attempts (exactly 1 here, the per-attempt hook) -- no trailing restore
    # that would destroy reviewable work the pipeline now owns.
    cfg = HarnessConfig()
    restore = RecordingRestore()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.PASSED, f"expected PASSED, got {r.outcome}"
    assert restore.calls == 1, \
        f"PASSED run must restore once (per-attempt only), saw {restore.calls}"
    assert not any(e["event"] == "tree_restored" for e in r.debate_log), \
        "a successful implement must not emit tree_restored"
    # Post-implement escalation: implement succeeds, then the gate fails.
    cfg = HarnessConfig()
    restore = RecordingRestore()
    orch = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}), None,
                gate_fail, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_GATE, \
        f"expected ESCALATED_GATE, got {r.outcome}"
    assert restore.calls == 1, \
        f"post-implement escalation must not add a trailing restore, saw {restore.calls}"
    assert not any(e["event"] == "tree_restored" for e in r.debate_log), \
        "a post-implement escalation must not emit tree_restored"

    # 38. INVARIANT (tree-hygiene): the clean-tree precondition refuses a dirty
    # tree and --allow-dirty opts out. Exercised on the extracted check function
    # directly (not the full CLI) in a temp git repo with a dirty tracked file.
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/f.txt", "w")).write("v1\n")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/f.txt", "w")).write("v2 DIRTY\n")  # dirty the tree
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            err = ensure_clean_tree(allow_dirty=False)
            err_allowed = ensure_clean_tree(allow_dirty=True)
        finally:
            os.chdir(prev_cwd)
    assert err is not None and "allow-dirty" in err, \
        f"dirty tree must refuse with an actionable message, got {err!r}"
    assert err_allowed is None, \
        f"--allow-dirty must bypass the precondition, got {err_allowed!r}"

    # 39. INVARIANT (tree-hygiene): real_restore_tree resets tracked changes and
    # removes untracked files, restoring the committed baseline exactly.
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/tracked.txt", "w")).write("BASELINE\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True)
        (open(f"{tmp}/tracked.txt", "w")).write("POISONED\n")  # dirty tracked
        (open(f"{tmp}/untracked.py", "w")).write("partial edit\n")  # untracked
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            await real_restore_tree()
            restored = open("tracked.txt").read()
            untracked_gone = not os.path.exists("untracked.py")
        finally:
            os.chdir(prev_cwd)
    assert restored == "BASELINE\n", \
        f"real_restore_tree must reset tracked content, got {restored!r}"
    assert untracked_gone, \
        "real_restore_tree must remove untracked files"

    # --- restore-boundary fix (follow-up to tree-hygiene) ---

    class FailOnCallRestore:
        """restore_tree stub that succeeds on the per-attempt call(s) and raises
        RuntimeError from the post-failure call, so a test can drive the
        _restore_after_failure guard without tripping the in-attempt path.
        Raises on every call at or after `fail_from` (1-indexed), mimicking a
        `git reset` that hit an index.lock collision."""
        def __init__(self, fail_from):
            self.calls = 0
            self.fail_from = fail_from
        async def __call__(self):
            self.calls += 1
            if self.calls >= self.fail_from:
                raise RuntimeError(
                    "fatal: Unable to create '.git/index.lock': File exists")

    # 40. INVARIANT (Member A): a post-failure restore that ITSELF fails must not
    # crash and must not mask the original escalation. Doer errors (per-attempt
    # restore, call 1, succeeds; doer raises); the post-failure restore (call 2)
    # raises RuntimeError. The run must still return a FeatureResult with the
    # ORIGINAL outcome (ESCALATED_NO_SIGNAL from the doer error, reason tagged
    # [doer]), and the debate log must carry a restore_failed event with the git
    # error. It must NOT raise out of run_feature.
    cfg = HarnessConfig()
    cfg.escalation.retries_before_counting = 0   # exactly one attempt
    restore = FailOnCallRestore(fail_from=2)     # post-failure call fails
    orch = make(cfg, ErroringImplementDoer({}),
                StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"failing post-failure restore must preserve the doer escalation, got {r.outcome}"
    assert r.escalation_reason and "[doer]" in r.escalation_reason, \
        f"original escalation must win (tagged [doer]): {r.escalation_reason!r}"
    rf = [e for e in r.debate_log if e["event"] == "restore_failed"]
    assert rf and rf[0]["reason"] == "implement_error" \
        and "index.lock" in rf[0]["error"], \
        f"a failed post-failure restore must log restore_failed(git error): {rf!r}"
    assert not any(e["event"] == "tree_restored" for e in r.debate_log), \
        "a FAILED restore must not also log tree_restored"

    # 41. INVARIANT (Member A): timeout variant of #40. Doer times out (one
    # attempt), the post-failure restore raises -> ESCALATED_TIMEOUT preserved,
    # restore_failed logged, no crash.
    class SleepingImplementDoer(StubDoer):
        async def implement(self, spec, acceptance):
            await asyncio.sleep(30)  # exceeds the positive step timeout below
            return "never"
    cfg = HarnessConfig()
    cfg.timeouts.model_call_seconds = 1          # positive: factory runs
    cfg.escalation.retries_before_counting = 0   # exactly one attempt
    cfg.escalation.consecutive_same_step_threshold = 5  # don't escalate early
    cfg.escalation.timeout_count_threshold = 5
    restore = FailOnCallRestore(fail_from=2)     # post-failure call fails
    orch = make(cfg, SleepingImplementDoer({}),
                StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_TIMEOUT, \
        f"failing post-failure restore must preserve the timeout escalation, got {r.outcome}"
    rf = [e for e in r.debate_log if e["event"] == "restore_failed"]
    assert rf and rf[0]["reason"] == "implement_timeout" \
        and "index.lock" in rf[0]["error"], \
        f"a failed post-failure restore must log restore_failed on timeout: {rf!r}"

    # 42. INVARIANT (Member A/Kimi): an IN-ATTEMPT restore failure attributes to
    # restore_tree, NOT the doer. The restore raises on the FIRST call (before
    # any implement attempt), so the doer never runs. Outcome is
    # ESCALATED_NO_SIGNAL tagged [restore_tree] -- blaming the doer for a git
    # failure would send the operator to debug the wrong component.
    cfg = HarnessConfig()
    restore = FailOnCallRestore(fail_from=1)     # per-attempt call fails
    orch = make(cfg, StubDoer({}),
                StubReviewer(cfg, {"issues": []}), None,
                gate_pass, diff_plain, restore_tree=restore)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"in-attempt restore failure must escalate NO_SIGNAL, got {r.outcome}"
    assert r.escalation_reason and "[restore_tree]" in r.escalation_reason, \
        f"in-attempt restore failure must be tagged [restore_tree]: {r.escalation_reason!r}"
    assert "[doer]" not in r.escalation_reason, \
        f"a restore failure must never be blamed on the doer: {r.escalation_reason!r}"

    # 43. INVARIANT (Member B): the clean-tree precondition REFUSES when the
    # check itself cannot prove the tree is clean. In a non-repo directory git
    # status exits non-zero with empty stdout; the old code read that empty
    # stdout as "clean" and silently passed. It must now return an actionable
    # message. Companion cases pin that the refusal is about PROVABILITY: a clean
    # repo returns None, a dirty one returns a message.
    with tempfile.TemporaryDirectory() as tmp:  # non-repo: refuse
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            non_repo_err = ensure_clean_tree(allow_dirty=False)
        finally:
            os.chdir(prev_cwd)
    assert non_repo_err is not None, \
        "a non-repo (failed git status) must refuse, not silently pass"
    with tempfile.TemporaryDirectory() as tmp:  # clean repo: pass
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        (open(f"{tmp}/f.txt", "w")).write("v1\n")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)
        prev_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            clean_err = ensure_clean_tree(allow_dirty=False)
            (open(f"{tmp}/f.txt", "w")).write("v2 DIRTY\n")  # now dirty
            dirty_err = ensure_clean_tree(allow_dirty=False)
        finally:
            os.chdir(prev_cwd)
    assert clean_err is None, \
        f"a clean repo must pass the precondition, got {clean_err!r}"
    assert dirty_err is not None and "allow-dirty" in dirty_err, \
        f"a dirty repo must refuse with an actionable message, got {dirty_err!r}"

    # 35. INVARIANT (Member A): deeply nested MODEL output escalates
    # ESCALATED_NO_SIGNAL instead of crashing. `json.loads` on pathologically
    # nested input raises RecursionError -- which is NOT a ValueError -- so the
    # ValueError-only narrowing would have let it crash the harness. The
    # boundary now catches (ValueError, RecursionError); a model emitting
    # nested-to-death JSON is bad model output (the no-signal bucket), not a
    # harness bug. We drive the REAL deep-input path (not a monkeypatch) so this
    # proves the actual failure mode: the reviewer returns `'['*N + ']'*N`,
    # which blows json.loads's recursion limit inside _parse_verdict.
    class DeepNestedReviewer(ReviewerClient):
        async def review(self, spec, acceptance, diff):
            return "[" * 200000 + "]" * 200000
    cfg = HarnessConfig()
    orch = make(cfg, StubDoer({}), DeepNestedReviewer(cfg), None,
                gate_pass, diff_plain)
    r = await orch.run_feature("spec", "acc")
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"deeply nested reviewer output must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[reviewer]" in r.escalation_reason, \
        f"escalation_reason must be tagged [reviewer]: {r.escalation_reason!r}"

    # 36. INVARIANT (Member A, class closure): a gate callable returning
    # nested-to-death JSON escalates ESCALATED_NO_SIGNAL "[gate]" instead of
    # crashing. GateResult.from_json_str calls json.loads, which raises
    # RecursionError on pathologically nested input -- NOT a ValueError -- so
    # before the tuple was widened this escaped run_feature and crashed the
    # harness. We drive the REAL deep-input path (a gate callable actually
    # returning `'['*N + ']'*N`), not a monkeypatch, so this pins the actual
    # failure mode at the GateResult parse boundary.
    cfg = HarnessConfig()
    async def gate_deep_nested():
        return "[" * 200000 + "]" * 200000
    orch = Orchestrator(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                        None, gate_deep_nested, diff_plain,
                        ab_swap=lambda _id: False)
    crashed = None
    try:
        r = await orch.run_feature("spec", "acc")
    except BaseException as e:  # must NOT raise
        crashed = e
    assert crashed is None, \
        f"nested-to-death gate output must not crash the harness, got {crashed!r}"
    assert r.outcome == Outcome.ESCALATED_NO_SIGNAL, \
        f"nested-to-death gate output must be no-signal, got {r.outcome}"
    assert r.escalation_reason and "[gate]" in r.escalation_reason, \
        f"escalation_reason must be tagged [gate]: {r.escalation_reason!r}"

    # 37. INVARIANT (Member B, class closure): _extract_codex_message skips a
    # nested-to-death JSONL line instead of crashing. A pathologically nested
    # line raises RecursionError inside json.loads; the per-line except now
    # includes RecursionError so the line is skipped like any other unparseable
    # line (continue semantics unchanged) and a LATER valid item.completed line
    # still wins (last-wins). Called directly to pin the extractor boundary.
    nested_line = "[" * 200000 + "]" * 200000
    valid_line = json.dumps(
        {"type": "item.completed", "item": {"text": "the real answer"}})
    extracted = _orch_mod._extract_codex_message(nested_line + "\n" + valid_line)
    assert extracted == "the real answer", \
        f"nested-to-death line must be skipped and later valid line win, got {extracted!r}"

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
        "tiebreaker_no_signal": (Outcome.ESCALATED_NO_SIGNAL, 2),
        "invariant_real_args": (Outcome.PASSED, 2),
        "invariant_transcript": (Outcome.PASSED, 2),
        "invariant_truncation": (Outcome.PASSED, 0),
        "invariant_silent_green_gate": (Outcome.PASSED, 0),
        "invariant_doer_timeout": (Outcome.ESCALATED_TIMEOUT, 0),
        "invariant_feature_budget": (Outcome.ABORTED_BUDGET, 0),
        "invariant_accepted_major_full_context": (Outcome.PASSED, 1),
        "invariant_major_deadlocks": (Outcome.ESCALATED_DISAGREEMENT, 2),
        # rounds_used=0 because the failure fires mid-round-1; per
        # completed-round semantics, the round didn't finish.
        "invariant_doer_malformed_response": (Outcome.ESCALATED_NO_SIGNAL, 0),
        "invariant_bare_empty_array_verdict_parses": (Outcome.PASSED, 0),
        "invariant_bare_populated_array_verdict_parses": (Outcome.PASSED, 1),
        "invariant_followup_bare_array_ends_debate_cleanly": (Outcome.PASSED, 1),
        "invariant_empty_doer_response_escalates": (
            Outcome.ESCALATED_NO_SIGNAL, 1),
        "invariant_partial_doer_response_escalates": (
            Outcome.ESCALATED_NO_SIGNAL, 1),
        "invariant_conformant_doer_response_still_passes": (Outcome.PASSED, 1),
        "gate_error_no_signal": (Outcome.ESCALATED_NO_SIGNAL, 0),
        "gate_malformed_no_signal": (Outcome.ESCALATED_NO_SIGNAL, 0),
        "gate_ran_and_failed": (Outcome.ESCALATED_GATE, 0),
        "empty_accept_skips_apply": (Outcome.PASSED, 1),
        # Reviewer prose fails mid-round-1 (like reviewer_no_signal); the
        # round didn't complete, so rounds_used=0.
        "invariant_reviewer_prose_escalates": (Outcome.ESCALATED_NO_SIGNAL, 0),
        # Malformed tiebreaker escalates after two held followups, mirroring
        # tiebreaker_no_signal's rounds_used=2.
        "invariant_tiebreaker_malformed_escalates": (
            Outcome.ESCALATED_NO_SIGNAL, 2),
        "invariant_verdict_array_shapes_parse": (Outcome.PASSED, 0),
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
