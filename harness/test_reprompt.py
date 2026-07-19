"""Pin slice B2: the ONE bounded re-prompt inside `Orchestrator._call_and_parse`.

B1 routed all five model-call-and-parse boundaries through one helper. B2 adds
a single bounded retry THERE and nowhere else: when the first parse raises,
re-prompt the SAME model ONCE -- stating the exact defect -- and parse the
reply; if that also fails, escalate EXACTLY as before B2. These tests hold the
mechanism to its contract:

  1. malformed-then-good returns the parsed value, for each of the five
     boundaries;
  2. the bound holds -- an always-malformed stub yields EXACTLY TWO client
     calls (original + one retry), never three (counted, asserted);
  3. on recurrence the escalation type/role/message are IDENTICAL to today
     (literals rebuilt from the same parser + prefixes the tree carries);
  4. timeouts and not-ok results are NOT parse failures -- exactly ONE model
     call, no retry;
  5. the retry note names the concrete defect -- AmbiguousJSON count+offsets
     from the exception ATTRIBUTES, a validation failure's field names;
  6. the note reaches the client AFTER slice A's schema tail;
  7. `_closure_sweep` still returns None on a twice-malformed reply and logs
     its existing `malformed` reason -- it does not start raising.

Slice B3 extends the same fixture with three more:

  8. (Item 1) a client whose method does NOT accept `retry_note` fails on the
     FIRST call, VISIBLY -- a loud TypeError before any parse or retry -- not
     deferred to the recovery path the way the deleted `_note_kw` hedge did;
  9. (Item 3) the per-message-type re-prompt counters and the one `reprompt`
     log event per retry: malformed-seen split by defect kind (ambiguity vs
     validation), re-prompts issued, cured vs recurred -- the event routed
     through `_log` so it gets the established `_pack_event`/`_truncate_walk`
     envelope, and the instrumentation changing no control flow;
 10. (Item 4) the two RECONSTRUCTED fixtures classify to the defect kinds they
     were reconstructed as -- and where a reconstruction no longer reproduces
     against today's tree, the test asserts THAT honestly rather than faking a
     raise.

The runner is a pass-through stub that ACTUALLY invokes the client coroutine
(so calls can be counted and the retry note captured), or returns a preset
StepResult to force timed_out / not-ok. No real model or CLI is involved.

SA-7 (both directions, probe evidence): each run prints the resolved `__file__`
of the modules it exercises, so "this would fail against the pre-B2 tree" is
EVIDENCED by which files were loaded, not assumed.
"""

import asyncio
import json

import harness.orchestrator as orchestrator
from harness.config import HarnessConfig, Backend
from harness.orchestrator import (
    Orchestrator,
    ReviewerClient,
    _assemble_prompt,
    _bounded_payload,
    _defect_kind,
    _noop_prompt_log,
    _parse_verdict,
    _parse_tiebreak,
    _retry_note,
    _schema_tail,
    _with_retry_note,
    _MSG_TYPES,
    _REVIEW_VERDICT_SCHEMA,
    _DOER_RESPONSE_SCHEMA,
    _CLOSURE_SCHEMA,
    _tiebreak_schema,
)
from harness.reprompt_fixtures import (
    SHAPE_1_DOER_WHERE_VERDICT,
    SHAPE_1_RECONSTRUCTED_DEFECT,
    SHAPE_1_REPRODUCES_TODAY,
    SHAPE_2_THREE_TOP_LEVEL_VALUES,
    SHAPE_2_RECONSTRUCTED_DEFECT,
    SHAPE_2_EXPECTED_COUNT,
    SHAPE_2_REPRODUCES_TODAY,
)
import harness_core.runner as core_runner
from harness_core.runner import StepResult, ModelUnavailable
from harness_core import jsonx
from harness.schemas import (
    ReviewVerdict, ReviewIssue, DoerResponse, IssueResponse, ClosureReport,
)


# --- runner + client stubs -------------------------------------------------

class PassRunner:
    """Executes the coroutine factory (the real StepRunner's success path) so
    the CLIENT is actually invoked -- letting a test COUNT client calls and
    CAPTURE the retry note. For any injected step it returns the preset
    StepResult instead (to force timed_out / not-ok), which -- like the real
    runner on a hang -- never invokes the client."""
    def __init__(self, injected=None):
        self.injected = injected or {}
        self.calls: list[str] = []

    async def run(self, step_name, coro_factory, timeout_seconds):
        self.calls.append(step_name)
        if step_name in self.injected:
            return self.injected[step_name]
        return StepResult(ok=True, output=await coro_factory())


def _pick(replies, i):
    """The i-th reply, saturating at the last -- so [BAD] means "always BAD"
    (both the original AND the retry) while [BAD, GOOD] recovers on the retry."""
    return replies[min(i, len(replies) - 1)]


class _Resp:
    """Stands in for a DoerResponse: the doer boundary serializes via
    `.model_dump_json()`, so a scripted string (malformed or valid) rides back
    through the exact same seam the real client uses."""
    def __init__(self, s): self._s = s
    def model_dump_json(self): return self._s


class ScriptedReviewer:
    """Reviewer whose review/respond/closure_scan return a SCRIPTED sequence and
    record the retry_note seen on each call. `_prompt_log` is set by the
    Orchestrator; declared so construction outside one still works."""
    def __init__(self, review=None, respond=None, closure=None):
        self._review = review or []
        self._respond = respond or []
        self._closure = closure or []
        self.review_notes: list = []
        self.respond_notes: list = []
        self.closure_notes: list = []
        self._prompt_log = _noop_prompt_log

    async def review(self, spec, acceptance, diff, retry_note=None):
        i = len(self.review_notes)
        self.review_notes.append(retry_note)
        return _pick(self._review, i)

    async def respond(self, spec, acceptance, diff, rejections, retry_note=None):
        i = len(self.respond_notes)
        self.respond_notes.append(retry_note)
        return _pick(self._respond, i)

    async def closure_scan(self, spec, diff, retry_note=None):
        i = len(self.closure_notes)
        self.closure_notes.append(retry_note)
        return _pick(self._closure, i)


class ScriptedDoer:
    def __init__(self, replies):
        self._replies = replies
        self.notes: list = []

    async def implement(self, spec, acceptance): return "implemented"

    async def respond_to_review(self, spec, acceptance, diff, verdict,
                                retry_note=None):
        i = len(self.notes)
        self.notes.append(retry_note)
        return _Resp(_pick(self._replies, i))

    async def apply_fixes(self, issues, diff): return "applied"


class ScriptedTiebreaker:
    def __init__(self, replies):
        self._replies = replies
        self.notes: list = []
        self._prompt_log = _noop_prompt_log

    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b,
                         retry_note=None):
        i = len(self.notes)
        self.notes.append(retry_note)
        return _pick(self._replies, i)


class _InertReviewer(ScriptedReviewer):
    """A reviewer that is present (Orchestrator wires its _prompt_log) but never
    exercised -- for tests whose boundary is the doer or tiebreaker."""
    def __init__(self):
        super().__init__(review=[json.dumps({"issues": []})],
                         respond=[json.dumps({"issues": []})],
                         closure=[json.dumps({"bug_class": "none",
                                              "patterns": []})])


class _InertDoer(ScriptedDoer):
    def __init__(self):
        super().__init__([json.dumps({"responses": []})])


def _make(*, reviewer=None, doer=None, tiebreaker=None, injected=None,
          ab_swap=None, cfg=None):
    cfg = cfg or HarnessConfig()

    async def _noop():
        return ""

    orch = Orchestrator(
        cfg,
        doer or _InertDoer(),
        reviewer or _InertReviewer(),
        tiebreaker,
        _noop, _noop,
        ab_swap=ab_swap or (lambda _id: False))
    orch.runner = PassRunner(injected)
    return orch


# _tiebreak needs I1 to be a held reviewer issue AND a rejected doer response.
_VERDICT = ReviewVerdict(issues=[
    ReviewIssue(id="I1", severity="blocking", issue="x", suggested_fix="y")])
_REJECTED = DoerResponse(responses=[
    IssueResponse(id="I1", decision="reject", reasoning="doer-reasoning")])

# A reply every parser rejects (no JSON at all).
_BAD = "this is not json at all"

# Per-boundary GOOD replies (the retry recovers to these).
_GOOD_VERDICT = json.dumps({"issues": [{"id": "I1", "severity": "blocking",
                                        "issue": "x", "suggested_fix": "y"}]})
_GOOD_EMPTY_VERDICT = json.dumps({"issues": []})
_GOOD_DOER = json.dumps({"responses": [{"id": "I1", "decision": "accept",
                                        "reasoning": "ok"}]})
_GOOD_TIEBREAK = json.dumps({"id": "I1", "sides_with": "a", "reasoning": "r"})
_GOOD_CLOSURE = json.dumps({"bug_class": "none", "patterns": []})


def _expected_malformed(prefix, parser, bad):
    """Rebuild the EXACT ModelUnavailable detail the recurrence path produces --
    `prefix` and the ` | payload (...)` framing copied verbatim from the tree,
    the `{e}` middle obtained by running the SAME parser on the SAME bytes. This
    is the pre-B2 escalation, unchanged: a twice-failed run looks like today's
    once-failed run."""
    try:
        parser(bad)
    except (ValueError, RecursionError) as e:
        body, plen = _bounded_payload(bad)
        return f"{prefix}{e} | payload ({plen} chars): {body}"
    raise AssertionError(f"{parser!r} did not reject the malformed reply")


# --- boundary invokers -----------------------------------------------------

async def _run_review(orch):
    return await orch._review("spec", "acc", "diff")


async def _run_followup(orch):
    return await orch._review_followup("spec", "acc", "diff", [])


async def _run_doer(orch):
    return await orch._doer_respond_to_review("spec", "acc", "diff", _VERDICT)


async def _run_tiebreak(orch):
    return await orch._tiebreak("spec", "acc", "diff", {"I1"},
                                _VERDICT, _REJECTED, 1)


async def _run_closure(orch):
    return await orch._closure_sweep("spec", "diff")


# --- tests -----------------------------------------------------------------

async def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    print(f"[probe] orchestrator under test: {orchestrator.__file__}")
    print(f"[probe] escalations/runner from: {core_runner.__file__}")
    print(f"[probe] ambiguity contract from: {jsonx.__file__}")

    # --- Test 1: malformed-then-good returns the parsed value (all five) ----
    # review
    rv = ScriptedReviewer(review=[_BAD, _GOOD_VERDICT])
    v = await _run_review(_make(reviewer=rv))
    check("t1 review: malformed->good returns verdict",
          isinstance(v, ReviewVerdict) and [i.id for i in v.issues] == ["I1"])
    check("t1 review: exactly one retry (2 calls)", len(rv.review_notes) == 2)
    # followup
    rv = ScriptedReviewer(respond=[_BAD, _GOOD_EMPTY_VERDICT])
    v = await _run_followup(_make(reviewer=rv))
    check("t1 followup: malformed->good returns verdict",
          isinstance(v, ReviewVerdict) and v.issues == [])
    check("t1 followup: exactly one retry (2 calls)", len(rv.respond_notes) == 2)
    # doer
    dr = ScriptedDoer([_BAD, _GOOD_DOER])
    r = await _run_doer(_make(doer=dr))
    check("t1 doer: malformed->good returns response",
          isinstance(r, DoerResponse) and [x.id for x in r.responses] == ["I1"])
    check("t1 doer: exactly one retry (2 calls)", len(dr.notes) == 2)
    # tiebreak (slot A is the doer under ab_swap=False; sides_with 'a' -> doer
    # wins -> I1 resolves -> empty still-blocking set)
    tb = ScriptedTiebreaker([_BAD, _GOOD_TIEBREAK])
    s = await _run_tiebreak(_make(tiebreaker=tb))
    check("t1 tiebreak: malformed->good resolves issue", s == set())
    check("t1 tiebreak: exactly one retry (2 calls)", len(tb.notes) == 2)
    # closure
    rv = ScriptedReviewer(closure=[_BAD, _GOOD_CLOSURE])
    rep = await _run_closure(_make(reviewer=rv))
    check("t1 closure: malformed->good returns a ClosureReport",
          isinstance(rep, ClosureReport) and rep.bug_class == "none")
    check("t1 closure: exactly one retry (2 calls)", len(rv.closure_notes) == 2)

    # --- Test 2: the bound holds -- always malformed -> EXACTLY TWO calls ----
    rv = ScriptedReviewer(review=[_BAD])
    try:
        await _run_review(_make(reviewer=rv))
    except ModelUnavailable:
        pass
    check("t2 review: always-malformed -> exactly 2 client calls, not 3",
          len(rv.review_notes) == 2)

    rv = ScriptedReviewer(respond=[_BAD])
    try:
        await _run_followup(_make(reviewer=rv))
    except ModelUnavailable:
        pass
    check("t2 followup: always-malformed -> exactly 2 client calls",
          len(rv.respond_notes) == 2)

    dr = ScriptedDoer([_BAD])
    try:
        await _run_doer(_make(doer=dr))
    except ModelUnavailable:
        pass
    check("t2 doer: always-malformed -> exactly 2 client calls",
          len(dr.notes) == 2)

    tb = ScriptedTiebreaker([_BAD])
    try:
        await _run_tiebreak(_make(tiebreaker=tb))
    except ModelUnavailable:
        pass
    check("t2 tiebreak: always-malformed -> exactly 2 client calls",
          len(tb.notes) == 2)

    rv = ScriptedReviewer(closure=[_BAD])
    await _run_closure(_make(reviewer=rv))  # logs-and-skips, never raises
    check("t2 closure: always-malformed -> exactly 2 client calls",
          len(rv.closure_notes) == 2)

    # --- Test 3: recurrence escalates IDENTICALLY to today ------------------
    for name, invoke, mk, role, prefix, parser in (
        ("review", _run_review,
         lambda: _make(reviewer=ScriptedReviewer(review=[_BAD])),
         "reviewer", "reviewer returned malformed response: ", _parse_verdict),
        ("followup", _run_followup,
         lambda: _make(reviewer=ScriptedReviewer(respond=[_BAD])),
         "reviewer", "reviewer returned malformed response: ", _parse_verdict),
        ("doer", _run_doer,
         lambda: _make(doer=ScriptedDoer([_BAD])),
         "doer", "doer returned malformed response: ",
         DoerResponse.model_validate_json),
        ("tiebreak", _run_tiebreak,
         lambda: _make(tiebreaker=ScriptedTiebreaker([_BAD])),
         "tiebreaker", "tiebreaker returned malformed response: ",
         _parse_tiebreak),
    ):
        raised = None
        try:
            await invoke(mk())
        except BaseException as e:  # noqa: BLE001 -- capture the exact escape
            raised = e
        want = _expected_malformed(prefix, parser, _BAD)
        check(f"t3 {name}: recurrence -> ModelUnavailable",
              isinstance(raised, ModelUnavailable))
        check(f"t3 {name}: role={role!r} unchanged",
              getattr(raised, "role", None) == role)
        check(f"t3 {name}: message literal unchanged",
              getattr(raised, "detail", None) == want)

    # --- Test 4: timeout and not-ok make EXACTLY ONE call (no retry) --------
    for name, step, invoke in (
        ("review", "reviewer:review", _run_review),
        ("doer", "doer:respond", _run_doer),
        ("tiebreak", "tiebreaker:adjudicate", _run_tiebreak),
        ("closure", "closure", _run_closure),
    ):
        for kind, res in (
            ("timeout", StepResult(ok=False, timed_out=True, error="x")),
            ("not-ok", StepResult(ok=False, error=None)),
        ):
            orch = _make(tiebreaker=ScriptedTiebreaker([_BAD]),
                         injected={step: res})
            try:
                await invoke(orch)
            except BaseException:  # noqa: BLE001 -- some boundaries raise
                pass
            n = orch.runner.calls.count(step)
            check(f"t4 {name}: {kind} -> exactly ONE model call (no retry)",
                  n == 1)

    # --- Test 5: the retry note names the concrete defect -------------------
    # (a) AmbiguousJSON: count + offsets from the exception ATTRIBUTES.
    ambiguous = _GOOD_EMPTY_VERDICT + "\n" + _GOOD_EMPTY_VERDICT
    amb_exc = None
    try:
        _parse_verdict(ambiguous)
    except jsonx.AmbiguousJSON as e:
        amb_exc = e
    rv = ScriptedReviewer(review=[ambiguous, _GOOD_VERDICT])
    await _run_review(_make(reviewer=rv))
    note = rv.review_notes[1]  # the note carried on the retry
    check("t5 ambiguous: note is not a bare 'try again'",
          note is not None and "try again" not in note.lower())
    check("t5 ambiguous: count from the exception attribute appears",
          str(amb_exc.count) in note)
    check("t5 ambiguous: every offset from the attribute appears",
          all(str(o) in note for o in amb_exc.offsets))

    # (b) validation failure: the offending field names, as pydantic reports.
    invalid = json.dumps({"issues": [{"id": "I1"}]})  # missing required fields
    val_exc = None
    try:
        _parse_verdict(invalid)
    except Exception as e:  # noqa: BLE001
        val_exc = e
    val_fields = set()
    for err in val_exc.errors():
        val_fields.add(".".join(str(p) for p in err.get("loc") or ()))
    rv = ScriptedReviewer(review=[invalid, _GOOD_VERDICT])
    await _run_review(_make(reviewer=rv))
    vnote = rv.review_notes[1]
    check("t5 validation: note names every offending field",
          bool(val_fields) and all(f in vnote for f in val_fields))

    # --- Test 6: the note reaches the client AFTER slice A's schema tail -----
    tail = _schema_tail(_REVIEW_VERDICT_SCHEMA)
    note6 = "CONCRETE-CORRECTION-NOTE"
    combined = _with_retry_note(tail, note6)
    check("t6 unit: tail precedes note", combined.index(tail) < combined.index(note6))
    check("t6 unit: note is the tail's terminus", combined.endswith(note6))
    check("t6 unit: None leaves the tail byte-identical",
          _with_retry_note(tail, None) == tail)

    # End-to-end: the REAL ReviewerClient folds the note past the schema tail,
    # and after stdin assembly the note is the final line -- after the diff AND
    # after the schema restatement (the recency argument the tail earns).
    cap = {}

    async def _fake_call_backend(backend, framing, diff, *, step, prompt_cfg,
                                 log=_noop_prompt_log, tail=""):
        cap["tail"] = tail
        return "{}"

    real = orchestrator.call_backend
    orchestrator.call_backend = _fake_call_backend
    try:
        await ReviewerClient(HarnessConfig()).review(
            "spec", "acc", "diff", retry_note=note6)
    finally:
        orchestrator.call_backend = real
    diff = "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-old\n+new\n" * 8
    prompt = _assemble_prompt(
        _STDIN_BACKEND, _review_framing_probe(), diff, step="probe",
        prompt_cfg=HarnessConfig().prompt, log=_noop_prompt_log,
        tail=cap["tail"])
    check("t6 e2e: schema tail present in the assembled prompt",
          tail in prompt)
    check("t6 e2e: note appears AFTER the schema tail",
          prompt.index(tail) < prompt.index(note6))
    check("t6 e2e: note is the last line of the prompt",
          prompt.rstrip("\n").splitlines()[-1] == note6)

    # --- Test 7: closure stays log-and-skip on a twice-malformed reply -------
    rv = ScriptedReviewer(closure=[_BAD])
    orch = _make(reviewer=rv)
    result = await _run_closure(orch)
    skips = [e for e in orch.log if e["event"] == "closure_skipped"]
    check("t7 closure: twice-malformed still returns None (does not raise)",
          result is None)
    check("t7 closure: logs its existing reason='malformed' exactly once",
          len(skips) == 1 and skips[0].get("reason") == "malformed")
    check("t7 closure: the retry happened (2 client calls)",
          len(rv.closure_notes) == 2)

    # --- Test 8 (Item 1): a client lacking `retry_note` fails on the FIRST
    # call, VISIBLY -- not deferred to the retry. `retry_note` now rides EVERY
    # call unconditionally (the `_note_kw` hedge is deleted), so a stub whose
    # method predates the param raises a loud TypeError on attempt one, before
    # any parse or retry. Under the hedge, attempt one omitted the kwarg, so a
    # legacy stub returning a VALID reply worked silently and the mismatch was
    # deferred to the rare recovery path -- the exact latent failure obs 013
    # names. This stub returns a VALID reply on purpose: proving the failure is
    # the CALL, not the content.
    class _LegacyDoer:
        async def implement(self, spec, acceptance): return "implemented"
        async def respond_to_review(self, spec, acceptance, diff, verdict):
            return _Resp(_GOOD_DOER)  # valid -- but never reached
        async def apply_fixes(self, issues, diff): return "applied"

    orch = _make(doer=_LegacyDoer())
    raised = None
    try:
        await _run_doer(orch)
    except BaseException as e:  # noqa: BLE001 -- capture the exact escape
        raised = e
    check("t8 legacy client: raises TypeError immediately",
          isinstance(raised, TypeError))
    check("t8 legacy client: the TypeError names retry_note",
          raised is not None and "retry_note" in str(raised))
    check("t8 legacy client: failed on the FIRST call (1 call), not the retry",
          orch.runner.calls.count("doer:respond") == 1)
    check("t8 legacy client: did NOT reach the escalation/recovery path",
          not isinstance(raised, ModelUnavailable))

    # --- Test 9 (Item 3): per-message-type counters + the one reprompt event --
    # (a) review, validation defect, cured on the retry.
    rv = ScriptedReviewer(review=[_BAD, _GOOD_VERDICT])
    orch = _make(reviewer=rv)
    v = await _run_review(orch)
    m = orch._reprompt_metrics["review"]
    check("t9a review: control flow unchanged -- still returns the verdict",
          isinstance(v, ReviewVerdict) and [i.id for i in v.issues] == ["I1"])
    check("t9a review: reprompts_issued==1, cured==1, recurred==0",
          m["reprompts_issued"] == 1 and m["cured"] == 1 and m["recurred"] == 0)
    check("t9a review: malformed_seen validation==1, ambiguity==0",
          m["malformed_seen"]["validation"] == 1
          and m["malformed_seen"]["ambiguity"] == 0)
    ev = [e for e in orch.log if e["event"] == "reprompt"]
    check("t9a review: exactly one reprompt event", len(ev) == 1)
    check("t9a review: event carries msg_type/defect/outcome",
          ev[0].get("msg_type") == "review" and ev[0].get("defect") == "validation"
          and ev[0].get("outcome") == "cured")
    check("t9a review: event has the established _pack_event envelope",
          all(k in ev[0] for k in ("event", "event_version", "ts")))

    # (b) doer, always-malformed -> recurred; BOTH replies are malformed-seen.
    dr = ScriptedDoer([_BAD])
    orch = _make(doer=dr)
    try:
        await _run_doer(orch)
    except ModelUnavailable:
        pass
    m = orch._reprompt_metrics["doer_response"]
    check("t9b doer: reprompts_issued==1, recurred==1, cured==0",
          m["reprompts_issued"] == 1 and m["recurred"] == 1 and m["cured"] == 0)
    check("t9b doer: malformed_seen validation==2 (both attempts counted)",
          m["malformed_seen"]["validation"] == 2)
    ev = [e for e in orch.log if e["event"] == "reprompt"]
    check("t9b doer: one reprompt event, outcome=recurred, type=doer_response",
          len(ev) == 1 and ev[0].get("outcome") == "recurred"
          and ev[0].get("msg_type") == "doer_response")

    # (c) ambiguity defect: an ambiguous first reply, cured on the retry.
    amb = _GOOD_EMPTY_VERDICT + "\n" + _GOOD_EMPTY_VERDICT
    rv = ScriptedReviewer(review=[amb, _GOOD_VERDICT])
    orch = _make(reviewer=rv)
    await _run_review(orch)
    m = orch._reprompt_metrics["review"]
    check("t9c ambiguity: malformed_seen ambiguity==1, validation==0",
          m["malformed_seen"]["ambiguity"] == 1
          and m["malformed_seen"]["validation"] == 0)
    ev = [e for e in orch.log if e["event"] == "reprompt"][0]
    check("t9c ambiguity: event defect=ambiguity, outcome=cured",
          ev.get("defect") == "ambiguity" and ev.get("outcome") == "cured")

    # (d) closure (the log-and-skip boundary) STILL emits a reprompt event.
    rv = ScriptedReviewer(closure=[_BAD])
    orch = _make(reviewer=rv)
    result = await _run_closure(orch)
    m = orch._reprompt_metrics["closure"]
    check("t9d closure: control flow unchanged -- still returns None",
          result is None)
    check("t9d closure: reprompts_issued==1, recurred==1",
          m["reprompts_issued"] == 1 and m["recurred"] == 1)
    ev = [e for e in orch.log if e["event"] == "reprompt"]
    check("t9d closure: reprompt event fired at the log-and-skip boundary",
          len(ev) == 1 and ev[0].get("msg_type") == "closure"
          and ev[0].get("outcome") == "recurred")

    # (e) instrumentation changes no control flow: a clean first parse emits NO
    # reprompt event and bumps NO counter.
    rv = ScriptedReviewer(review=[_GOOD_VERDICT])
    orch = _make(reviewer=rv)
    await _run_review(orch)
    check("t9e clean: no reprompt event, no counter bump on a good first reply",
          not any(e["event"] == "reprompt" for e in orch.log)
          and orch._reprompt_metrics["review"]["reprompts_issued"] == 0)

    # (f) the counter structure is dense: every message type keyed, both defect
    # kinds pre-seeded -- a measured zero is never a missing key (scope-of-claim).
    orch = _make()
    check("t9f structure: all five message types keyed, defect kinds pre-seeded",
          set(orch._reprompt_metrics) == set(_MSG_TYPES)
          and all(set(mv["malformed_seen"]) == {"ambiguity", "validation"}
                  for mv in orch._reprompt_metrics.values()))

    # --- Test 10 (Item 4): the RECONSTRUCTED fixtures classify to the defect
    # kinds they were reconstructed as -- honestly, including where one no
    # longer reproduces against today's tree.
    # shape 2: three top-level values -> ambiguity; still reproduces.
    amb_exc = None
    try:
        _parse_verdict(SHAPE_2_THREE_TOP_LEVEL_VALUES)
    except (ValueError, RecursionError) as e:
        amb_exc = e
    check("t10 shape2: reproduces as AmbiguousJSON with count==3",
          isinstance(amb_exc, jsonx.AmbiguousJSON)
          and amb_exc.count == SHAPE_2_EXPECTED_COUNT)
    check("t10 shape2: classifies to its reconstructed defect kind (ambiguity)",
          _defect_kind(amb_exc) == SHAPE_2_RECONSTRUCTED_DEFECT == "ambiguity")
    check("t10 shape2: fixture self-labels as reproducing today",
          SHAPE_2_REPRODUCES_TODAY is True)
    # shape 1: doer-shaped where a verdict was required. Reconstructed as a
    # validation defect, but it NO LONGER raises -- assert the honest behaviour
    # the fixture documents, not a pretend raise.
    verdict1 = _parse_verdict(SHAPE_1_DOER_WHERE_VERDICT)
    check("t10 shape1: does NOT raise today (extra keys ignored, issues=[])",
          isinstance(verdict1, ReviewVerdict) and verdict1.issues == [])
    check("t10 shape1: fixture self-labels reconstructed=validation, not reproducing",
          SHAPE_1_REPRODUCES_TODAY is False
          and SHAPE_1_RECONSTRUCTED_DEFECT == "validation")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


# A stdin backend + minimal framing for the test-6 end-to-end assembly. Mirrors
# the real reviewer delivery (stdin, no packing) so the assembled prompt is the
# one the model would actually read.
_STDIN_BACKEND = Backend("probe", ["true"], "text", stdin=True)


def _review_framing_probe() -> str:
    return "REVIEW FRAMING\n\nDIFF:\n"


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
