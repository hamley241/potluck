"""Slice A of TASK-reprompt-on-contract-violation: make malformation RARE
(schema restated at the prompt TAIL) and EVIDENCED (the offending payload is
captured, truncated + length, at every parse boundary).

These are deterministic harness contracts -- no real model calls. We drive the
prompt builders and parse boundaries directly, capturing what the production
client wiring actually passes so the assertions pin the REAL path, not a
re-derivation of it.

Self-contained: it reuses stubs/fixtures from the sibling suites by import but
edits none of them (slice A adds evidence, never changes an existing outcome).
"""

import asyncio
import json

from harness import orchestrator as orch
from harness.config import HarnessConfig, Backend
from harness.orchestrator import (
    ReviewerClient, TiebreakerClient, RealDoerClient,
    _assemble_prompt, _review_framing, _review_followup_framing,
    _tiebreak_framing, _schema_tail, _tiebreak_schema,
    _REVIEW_VERDICT_SCHEMA, _DOER_RESPONSE_SCHEMA, _MAX_STR,
)
from harness.schemas import (
    DoerResponse, IssueResponse, ReviewVerdict, ReviewIssue, Outcome,
)

# Sibling scaffolding -- imported, never edited.
from harness.test_orchestrator import (
    StubDoer, StubReviewer, make, gate_pass, diff_plain,
)
from harness.test_closure import (
    ClosureReviewer, StubDoer as ClosureStubDoer, make as closure_make,
    gate_pass as closure_gate_pass, diff_for, _init_repo, _run_in_repo,
)


STDIN_BACKEND = Backend("claude", ["claude", "-p"], "text", stdin=True)
_TAIL_PREFIX = "Return exactly ONE JSON value matching: "


def _last_line(prompt: str) -> str:
    lines = prompt.splitlines()
    return lines[-1] if lines else ""


def _stdin_prompt(framing: str, diff: str, tail: str) -> str:
    return _assemble_prompt(
        STDIN_BACKEND, framing, diff, step="probe",
        prompt_cfg=HarnessConfig().prompt, log=orch._noop_prompt_log, tail=tail)


async def _capture_client(call) -> dict:
    """Run a ReviewerClient/TiebreakerClient coroutine with call_backend
    stubbed, capturing the (framing, diff, tail) the client ACTUALLY passed --
    so we assert the production wiring, not a hand-rebuilt copy of it."""
    cap: dict = {}

    async def fake(backend, framing, diff, *, step, prompt_cfg,
                   log=None, tail=""):
        cap.update(framing=framing, diff=diff, tail=tail)
        return "{}"

    real = orch.call_backend
    orch.call_backend = fake
    try:
        await call()
    finally:
        orch.call_backend = real
    return cap


async def _capture_doer_prompt(payload_reply: str) -> str:
    """Drive RealDoerClient.respond_to_review with run_subprocess stubbed,
    returning the exact prompt string sent on stdin."""
    cap: dict = {}

    async def fake_run(cmd, stdin_text=None):
        cap["prompt"] = stdin_text
        return payload_reply

    real = orch.run_subprocess
    orch.run_subprocess = fake_run
    try:
        verdict = ReviewVerdict(issues=[ReviewIssue(
            id="I1", severity="blocking", issue="x", suggested_fix="y")])
        await RealDoerClient(claude_cmd="unused").respond_to_review(
            "spec", "acc", "diff", verdict)
    finally:
        orch.run_subprocess = real
    return cap["prompt"]


# --- Test 1: the tail is the LAST line on all FOUR message types ------------

async def test_tail_is_last_line_all_four(check) -> None:
    cfg = HarnessConfig()
    rejections = DoerResponse(responses=[
        IssueResponse(id="I1", decision="reject", reasoning="no")])

    # (a-c) the three diff-carrying types: capture the client's framing+tail,
    # then assemble the real stdin prompt (framing + diff + tail) and confirm
    # the tail is the final line -- AFTER the diff, which is the whole point.
    cases = {
        "review": (
            lambda: ReviewerClient(cfg).review("spec", "acc", "diff"),
            _schema_tail(_REVIEW_VERDICT_SCHEMA)),
        "followup": (
            lambda: ReviewerClient(cfg).respond(
                "spec", "acc", "diff", rejections),
            _schema_tail(_REVIEW_VERDICT_SCHEMA)),
        "tiebreak": (
            lambda: TiebreakerClient(cfg).adjudicate(
                "spec", "acc", "diff", "I7", "arg a", "arg b"),
            _schema_tail(_tiebreak_schema("I7"))),
    }
    diff = ("diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-old\n+new\n") * 40
    for name, (call, expected_tail) in cases.items():
        cap = await _capture_client(call)
        check(f"tail_passed:{name}", cap["tail"] == expected_tail)
        check(f"tail_shape:{name}", cap["tail"].startswith(_TAIL_PREFIX))
        prompt = _stdin_prompt(cap["framing"], diff, cap["tail"])
        check(f"tail_is_last_line:{name}", _last_line(prompt) == expected_tail)
        # The diff really is BEFORE the tail (recency argument holds).
        check(f"tail_after_diff:{name}",
              prompt.index(diff.rstrip("\n")) < prompt.rindex(expected_tail))

    # (d) doer response: monolithic prompt, tail already terminal.
    valid = json.dumps({"responses": [
        {"id": "I1", "decision": "reject", "reasoning": "r"}]})
    doer_prompt = await _capture_doer_prompt(valid)
    check("tail_is_last_line:doer",
          _last_line(doer_prompt) == _schema_tail(_DOER_RESPONSE_SCHEMA))
    check("tail_shape:doer",
          _last_line(doer_prompt).startswith(_TAIL_PREFIX))


# --- Test 2: schema REUSE is real (same value + injection) ------------------

async def test_schema_reuse_and_injection(check) -> None:
    cfg = HarnessConfig()
    rejections = DoerResponse(responses=[
        IssueResponse(id="I1", decision="reject", reasoning="no")])

    # review + followup share ONE constant; assert it appears in BOTH the body
    # and the tail, then INJECT a sentinel into the source and watch both move.
    for name, call in (
        ("review", lambda: ReviewerClient(cfg).review("spec", "acc", "diff")),
        ("followup", lambda: ReviewerClient(cfg).respond(
            "spec", "acc", "diff", rejections)),
    ):
        cap = await _capture_client(call)
        check(f"reuse_body:{name}", _REVIEW_VERDICT_SCHEMA in cap["framing"])
        check(f"reuse_tail:{name}", _REVIEW_VERDICT_SCHEMA in cap["tail"])

    sentinel = '{"SENTINEL_REVIEW": true}'
    real = orch._REVIEW_VERDICT_SCHEMA
    orch._REVIEW_VERDICT_SCHEMA = sentinel
    try:
        cap = await _capture_client(
            lambda: ReviewerClient(cfg).review("spec", "acc", "diff"))
        check("inject_review_body", sentinel in cap["framing"])
        check("inject_review_tail", sentinel in cap["tail"])
        check("inject_review_no_stale",
              real not in cap["framing"] and real not in cap["tail"])
    finally:
        orch._REVIEW_VERDICT_SCHEMA = real

    # tiebreak: schema is parameterized by issue_id via one builder. Body and
    # tail both go through it, so patching the builder moves both.
    cap = await _capture_client(lambda: TiebreakerClient(cfg).adjudicate(
        "spec", "acc", "diff", "I7", "a", "b"))
    check("reuse_body:tiebreak", _tiebreak_schema("I7") in cap["framing"])
    check("reuse_tail:tiebreak", _tiebreak_schema("I7") in cap["tail"])

    def fake_tb_schema(issue_id):
        return f'{{"SENTINEL_TB": "{issue_id}"}}'
    real_tb = orch._tiebreak_schema
    orch._tiebreak_schema = fake_tb_schema
    try:
        cap = await _capture_client(lambda: TiebreakerClient(cfg).adjudicate(
            "spec", "acc", "diff", "I7", "a", "b"))
        check("inject_tb_body", '"SENTINEL_TB": "I7"' in cap["framing"])
        check("inject_tb_tail", '"SENTINEL_TB": "I7"' in cap["tail"])
    finally:
        orch._tiebreak_schema = real_tb

    # doer: the monolithic prompt states the schema twice (body + tail).
    valid = json.dumps({"responses": [
        {"id": "I1", "decision": "reject", "reasoning": "r"}]})
    doer_prompt = await _capture_doer_prompt(valid)
    check("reuse_doer_twice", doer_prompt.count(_DOER_RESPONSE_SCHEMA) == 2)

    sentinel_d = '{"SENTINEL_DOER": true}'
    real_d = orch._DOER_RESPONSE_SCHEMA
    orch._DOER_RESPONSE_SCHEMA = sentinel_d
    try:
        doer_prompt = await _capture_doer_prompt(valid)
        check("inject_doer_twice", doer_prompt.count(sentinel_d) == 2)
        check("inject_doer_no_stale", real_d not in doer_prompt)
    finally:
        orch._DOER_RESPONSE_SCHEMA = real_d


# --- Test 3: payload CAPTURED and BOUNDED at the parse boundaries ------------

async def test_payload_captured_and_bounded(check) -> None:
    L = _MAX_STR + 500
    payload = "Z" * L  # not valid JSON -> every parse boundary rejects it

    # (a) the RAISE path that carries only a RuntimeError (RealDoerClient):
    #     the offending reply must reach the exception detail, truncated to
    #     _MAX_STR with its original length recorded.
    real = orch.run_subprocess

    async def fake_run(cmd, stdin_text=None):
        return payload
    orch.run_subprocess = fake_run
    raised = None
    try:
        verdict = ReviewVerdict(issues=[ReviewIssue(
            id="I1", severity="blocking", issue="x", suggested_fix="y")])
        await RealDoerClient(claude_cmd="unused").respond_to_review(
            "spec", "acc", "diff", verdict)
    except RuntimeError as e:
        raised = e
    finally:
        orch.run_subprocess = real
    detail = str(raised)
    check("doer_raise_is_malformed", raised is not None and "malformed" in detail)
    check("doer_raise_records_len", f"({L} chars)" in detail)
    check("doer_raise_truncated_present", ("Z" * _MAX_STR) in detail)
    check("doer_raise_cap_holds", ("Z" * (_MAX_STR + 1)) not in detail)

    # (b) the RAISE path that becomes an escalation reason (reviewer, via
    #     ModelUnavailable): the payload must reach FeatureResult.escalation_reason,
    #     and the [reviewer] tag / NO_SIGNAL outcome must be UNCHANGED.
    class _OversizeReviewer(ReviewerClient):
        async def review(self, spec, acceptance, diff):
            return payload

        async def respond(self, spec, acceptance, diff, rejections):
            return json.dumps({"issues": []})

        async def closure_scan(self, spec, diff):
            return json.dumps({"bug_class": "none", "patterns": []})

    cfg = HarnessConfig()
    orch_obj = make(cfg, StubDoer({}), _OversizeReviewer(cfg), None,
                    gate_pass, diff_plain)
    r = await orch_obj.run_feature("spec", "acc")
    reason = r.escalation_reason or ""
    check("reviewer_escalates_no_signal",
          r.outcome == Outcome.ESCALATED_NO_SIGNAL)
    check("reviewer_tag_unchanged", "[reviewer]" in reason)
    check("reviewer_records_len", f"({L} chars)" in reason)
    check("reviewer_truncated_present", ("Z" * _MAX_STR) in reason)
    check("reviewer_cap_holds", ("Z" * (_MAX_STR + 1)) not in reason)

    # (c) the LOG path (closure, the one boundary that logs not raises): the
    #     payload must land in the closure_skipped event, truncated + length,
    #     and the run must still PASS (advisory, unchanged).
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        ccfg = HarnessConfig()
        reviewer = ClosureReviewer(ccfg, payload)
        corch = closure_make(ccfg, ClosureStubDoer(), reviewer,
                             closure_gate_pass, diff_for(["x.py"]))
        rc = await _run_in_repo(tmp, corch)
    skips = [e for e in rc.debate_log if e["event"] == "closure_skipped"]
    check("closure_still_passed", rc.outcome == Outcome.PASSED)
    check("closure_reason_unchanged",
          bool(skips) and skips[0].get("reason") == "malformed")
    # The oversized payload rides the standard debate-log shape: the field is
    # truncated to _MAX_STR and the original length lands in the `truncations`
    # sibling keyed by field path -- NOT a bespoke payload_len.
    check("closure_records_len",
          bool(skips) and skips[0].get("truncations", {}).get("payload") == L)
    check("closure_cap_holds",
          bool(skips) and skips[0].get("payload") == "Z" * _MAX_STR)
    check("closure_no_bespoke_len",
          bool(skips) and "payload_len" not in skips[0])


# --- Test 4: no behaviour change -- a clean review still PASSES --------------

async def test_no_behaviour_change(check) -> None:
    # A well-formed empty verdict must still early-exit PASSED: the tail and the
    # payload capture add evidence, never a new outcome or recovery.
    cfg = HarnessConfig()
    orch_obj = make(cfg, StubDoer({}), StubReviewer(cfg, {"issues": []}),
                    None, gate_pass, diff_plain)
    r = await orch_obj.run_feature("spec", "acc")
    check("clean_review_still_passes", r.outcome == Outcome.PASSED)


async def main() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    print("=== contract tail + payload evidence (slice A) ===")
    await test_tail_is_last_line_all_four(check)
    await test_schema_reuse_and_injection(check)
    await test_payload_captured_and_bounded(check)
    await test_no_behaviour_change(check)
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
