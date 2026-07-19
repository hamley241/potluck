"""Class-closure sweep: after a fix PASSES, the reviewer backend names the bug
CLASS it closed and emits grep patterns; the HARNESS runs them and reports only
real file:line matches it found. These tests pin the whole contract:

  1. happy path end-to-end (a planted sibling is found and reported)
  2. Contract A, honestly -- a report attaches with real grep candidates while
     a model-claimed path (in bug_class/rationale) never becomes a candidate
  3. files touched by the diff are excluded (those are the fixed sites)
  4. advisory -- a failing sweep can never break a PASSED run
  5. disabled by config -- no model call, no closure events
  6. a clean sweep is still recorded (the question was asked)
  7. caps (5 patterns, 20 candidates) are visible, never silent truncation
  8. the FINAL (step-9) PASSED path -- reached through a real debate round,
     apply_fixes, and the post-fix gate -- attaches the report and logs
     closure_report / closure_clean too, not just the early-exit path
  9. every free-text field on the report is bounded so it can't blow the
     worst-case FeatureResult size on its way into --json-output:
       9.  a real 50k-char grep line is capped with an elision marker
       10. model-supplied bug_class + rationale are likewise bounded
       11. an over-long regex is REJECTED (never truncated): it never runs, is
           filtered OUT of closure_report.patterns even when a valid sibling
           pattern makes a report attach, a closure_skipped(scope=pattern) that
           carries only its LENGTH (not the raw regex) is logged, and no
           truncated form of the regex appears anywhere in the report or log --
           even when the regex is longer than the _pack_event field cap
 12. signal death is not success -- a grep killed by a signal (negative
     returncode) skips the pattern and yields no candidate from partial stdout
 13. caps bound the WORK -- the pattern cap applies before per-pattern
     validation (a malformed entry past the cap is never validated), and the
     capped-count log still reports the true `returned` count

Tests 1-7 all converge through the early-exit PASSED branch (review finds
nothing blocking), so on their own they leave the second wiring point unpinned.
Test 8 is that OTHER wiring point: a reviewer that raises a blocking issue, a
doer that accepts it, fixes applied, gate re-run -- so a regression where the
sweep stops attaching on the final PASSED path is caught.

The sweep greps the real working tree, so tests run inside a temp git repo with
planted files. No real model calls: the reviewer's closure reply is scripted.
"""

import asyncio
import json
import os
import subprocess
import tempfile

from harness import orchestrator as orch_mod
from harness.config import HarnessConfig
from harness.orchestrator import (
    Orchestrator, DoerClient, ReviewerClient, GateResult,
    _CLOSURE_MAX_TEXT, _bound_closure_text, _MAX_STR,
)
from harness.schemas import DoerResponse, IssueResponse, Outcome


# --- stubs ---------------------------------------------------------------

class StubDoer(DoerClient):
    async def implement(self, spec, acceptance): return "implemented"
    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        return DoerResponse(responses=[])
    async def apply_fixes(self, issues, diff): return "applied"


class AcceptingDoer(StubDoer):
    """Doer that ACCEPTS every issue the reviewer raises, driving the debate
    loop through apply_fixes + the post-fix gate to the FINAL (step-9) PASSED
    return -- the second place the closure sweep must attach. Records
    apply_fixes calls so a test can confirm fixes really flowed.

    Wired to a `MutatingDiff` so apply_fixes observably changes the tree: the
    inaction guarantee (orchestrator's fifth control guarantee) now escalates an
    accepted issue whose apply produced no edit, so a step-9 stub that leaves the
    diff untouched would (correctly) escalate instead of PASSED. Modeling a real
    apply is what lets these tests reach the FINAL PASSED they are asserting on."""
    def __init__(self, diff):
        self.apply_fixes_calls: list[dict] = []
        self._diff = diff
        self._napply = 0

    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision="accept", reasoning="stub")
            for i in verdict.issues])

    async def apply_fixes(self, issues, diff):
        self.apply_fixes_calls.append(
            {"issues": [i.model_dump() for i in issues]})
        # Advance the tree diff so the post-apply capture DIFFERS from pre-apply
        # (observable change discharges the accepted-issue obligation).
        self._napply += 1
        self._diff.state += f"@@ applied {self._napply} @@\n+fix\n"
        return "applied"


class ClosureReviewer(ReviewerClient):
    """Reviewer that returns a SCRIPTED first-review verdict and a SCRIPTED
    closure reply. `review_reply` defaults to an empty verdict (nothing
    blocking -> early-exit PASSED); pass one with a blocking/major issue to
    drive the full debate path to the final PASSED. `closure_reply` may be a
    dict/list (json-encoded), a raw str (fed verbatim -- for the prose/malformed
    case), or an Exception instance (raised, for the model-errored case).
    Records call count so a test can assert the sweep was or wasn't invoked."""
    def __init__(self, cfg, closure_reply, review_reply=None):
        super().__init__(cfg)
        self.closure_reply = closure_reply
        self.review_reply = (review_reply if review_reply is not None
                             else {"issues": []})
        self.closure_calls = 0

    async def review(self, spec, acceptance, diff, retry_note=None):
        return json.dumps(self.review_reply)

    async def respond(self, spec, acceptance, diff, rejections, retry_note=None):
        return json.dumps({"issues": []})

    async def closure_scan(self, spec, diff, retry_note=None):
        self.closure_calls += 1
        r = self.closure_reply
        if isinstance(r, BaseException):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


async def gate_pass():
    return (True, "green")


def make(cfg, doer, reviewer, gate, diff):
    async def run_gate():
        ok, out = await gate()
        return GateResult(ok=ok, output=out).to_json_str()
    return Orchestrator(cfg, doer, reviewer, None, run_gate, diff,
                        ab_swap=lambda _id: False)


def _diff_header(paths):
    """The `diff --git` header text naming each of `paths`."""
    return "".join(
        f"diff --git a/{p} b/{p}\n@@ -1 +1 @@\n-old\n+new\n" for p in paths)


def diff_for(paths):
    """An async get_diff returning a diff whose `diff --git` headers name each
    of `paths` (the sites just fixed -- the sweep must exclude them)."""
    header = _diff_header(paths)

    async def _diff():
        return header
    return _diff


class MutatingDiff:
    """A stateful get_diff whose output can be advanced by a wired doer, so the
    step-9 tests can model apply_fixes making a real edit -- required now that
    the orchestrator escalates an accepted issue whose apply produced no
    observable change. Seeded with the touched-path header so the closure sweep
    (which reuses the step-3 diff) still sees the fixed sites to exclude."""
    def __init__(self, paths):
        self.state = _diff_header(paths)

    async def __call__(self):
        return self.state


def _init_repo(tmp, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
    for name, content in files.items():
        path = os.path.join(tmp, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)


async def _run_in_repo(tmp, orch, spec="spec", acc="acc"):
    prev = os.getcwd()
    try:
        os.chdir(tmp)
        return await orch.run_feature(spec, acc)
    finally:
        os.chdir(prev)


def _closure_events(log):
    return [e for e in log if e["event"].startswith("closure")]


async def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1. HAPPY PATH: a planted sibling matching a reviewer-supplied regex is
    # found by the harness and reported; a closure_report event is logged.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {
            "sibling.py": "def foo():\n    proc.communicate()  # SIBLING_MARKER\n",
        })
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "unchecked subprocess result",
            "patterns": [{"regex": "SIBLING_MARKER",
                          "rationale": "same unchecked-result shape"}],
        })
        orch = make(cfg, StubDoer(), reviewer, gate_pass,
                    diff_for(["src/feature.py"]))
        r = await _run_in_repo(tmp, orch)
    check("happy_passed", r.outcome == Outcome.PASSED)
    check("happy_report_attached", r.closure_report is not None)
    cand = [(c.file, c.line) for c in (r.closure_report.candidates
                                       if r.closure_report else [])]
    check("happy_candidate_found", ("sibling.py", 2) in cand)
    check("happy_scan_called_once", reviewer.closure_calls == 1)
    rep_ev = [e for e in r.debate_log if e["event"] == "closure_report"]
    check("happy_report_event", len(rep_ev) == 1)
    check("happy_report_event_fields",
          bool(rep_ev)
          and rep_ev[0]["bug_class"] == "unchecked subprocess result"
          and rep_ev[0]["candidate_count"] == 1
          and any(c["file"] == "sibling.py" and c["line"] == 2
                  and c["pattern"] == "SIBLING_MARKER"
                  for c in rep_ev[0]["candidates"]))

    # 2. CONTRACT A, honestly. Candidates are HARNESS-VERIFIED grep output; the
    # model's bug_class and rationale are unverified EXPLANATION that MAY carry a
    # path -- but a model-claimed path must NEVER become a candidate. The old
    # version of this test used a regex matching nothing, so no report attached
    # at all; it would have passed even if an invented path leaked into an
    # attached report. This version makes the reviewer return a pattern that DOES
    # match a planted line (so a report genuinely attaches with candidates) and
    # stuffs an invented path `src/ghost.py:12` into BOTH bug_class and
    # rationale. We assert: (a) a report attaches with >= 1 candidate; (b) every
    # candidate's file is a real path in the repo under test; (c) the invented
    # path appears in NO candidate's file/text -- it MAY appear in
    # bug_class/rationale (that is the honest contract now), so we assert its
    # absence specifically from the candidate fields.
    invented = "src/ghost.py:12"
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"real.py": "def foo():\n    CONTRACTA_MARKER here\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": f"unchecked result -- sibling at {invented}",
            "patterns": [{"regex": "CONTRACTA_MARKER",
                          "rationale": f"the sibling lives at {invented}"}],
        })
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["other.py"]))
        r = await _run_in_repo(tmp, orch)
        cands = list(r.closure_report.candidates) if r.closure_report else []
        # (b) every candidate file is a real path in the repo under test.
        files_real = bool(cands) and all(
            os.path.exists(os.path.join(tmp, c.file)) for c in cands)
    check("contracta_passed", r.outcome == Outcome.PASSED)
    # (a) a report IS attached with at least one candidate.
    check("contracta_report_attached",
          r.closure_report is not None and len(cands) >= 1)
    check("contracta_candidate_files_real", files_real)
    # (c) the invented path never appears in any candidate's file or text.
    check("contracta_invented_absent_from_candidates",
          all(invented not in c.file and invented not in c.text
              and "ghost.py" not in c.file and "ghost.py" not in c.text
              for c in cands))
    # The invented path IS allowed to appear in bug_class/rationale -- confirm
    # the report really did carry the model's words (so (c) is meaningful, not
    # vacuously true because nothing model-supplied was retained).
    rep = r.closure_report
    check("contracta_model_text_retained",
          rep is not None
          and invented in rep.bug_class
          and any(invented in p.rationale for p in rep.patterns))

    # 3. TOUCHED FILES EXCLUDED: a match inside a file the diff touches is the
    # just-fixed site and must NOT be reported; the untouched sibling must.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {
            "touched.py": "MATCH_MARKER lives here\n",
            "untouched.py": "MATCH_MARKER lives here too\n",
        })
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": "MATCH_MARKER", "rationale": "r"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass,
                    diff_for(["touched.py"]))
        r = await _run_in_repo(tmp, orch)
    reported = sorted(c.file for c in (r.closure_report.candidates
                                       if r.closure_report else []))
    check("touched_only_untouched_reported", reported == ["untouched.py"])

    # 4. ADVISORY: a failing sweep can never break a PASSED run. Three flavors,
    # each PASSED with a closure_skipped event logged.
    # (a) the closure model call errors.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, RuntimeError("claude exited 1: no auth"))
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        r = await _run_in_repo(tmp, orch)
    check("advisory_error_passed", r.outcome == Outcome.PASSED)
    check("advisory_error_skipped",
          any(e["event"] == "closure_skipped" for e in r.debate_log))

    # (b) it returns prose (no JSON) -> malformed.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, "I think everything looks fine here.")
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        r = await _run_in_repo(tmp, orch)
    skip_ev = [e for e in r.debate_log if e["event"] == "closure_skipped"]
    check("advisory_prose_passed", r.outcome == Outcome.PASSED)
    check("advisory_prose_skipped_malformed",
          bool(skip_ev) and skip_ev[0].get("reason") == "malformed")

    # (c) a returned regex is invalid so `git grep` exits > 1.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": "[unterminated", "rationale": "r"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        r = await _run_in_repo(tmp, orch)
    bad_skip = [e for e in r.debate_log if e["event"] == "closure_skipped"]
    check("advisory_badregex_passed", r.outcome == Outcome.PASSED)
    check("advisory_badregex_skipped",
          any(e.get("regex") == "[unterminated" for e in bad_skip))

    # 5. DISABLED BY CONFIG: no closure model call is made and no closure events
    # are logged.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"sibling.py": "SIBLING_MARKER\n"})
        cfg = HarnessConfig()
        cfg.closure_enabled = False
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": "SIBLING_MARKER", "rationale": "r"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["a.py"]))
        r = await _run_in_repo(tmp, orch)
    check("disabled_passed", r.outcome == Outcome.PASSED)
    check("disabled_no_scan_call", reviewer.closure_calls == 0)
    check("disabled_no_closure_events", _closure_events(r.debate_log) == [])
    check("disabled_report_none", r.closure_report is None)

    # 6. CLEAN SWEEP RECORDED: the model answers "none" -> PASSED, a
    # closure_clean event is logged, and closure_report is None (our convention).
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {"bug_class": "none", "patterns": []})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        r = await _run_in_repo(tmp, orch)
    clean_ev = [e for e in r.debate_log if e["event"] == "closure_clean"]
    check("clean_passed", r.outcome == Outcome.PASSED)
    check("clean_event_logged", len(clean_ev) == 1)
    check("clean_report_none", r.closure_report is None)
    check("clean_no_report_event",
          not any(e["event"] == "closure_report" for e in r.debate_log))

    # 7. CAPS ARE VISIBLE.
    # (a) more than 5 patterns -> excess dropped AND recorded.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": f"NOMATCH_{i}", "rationale": "r"}
                         for i in range(7)]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        r = await _run_in_repo(tmp, orch)
    pcap = [e for e in r.debate_log if e["event"] == "closure_patterns_capped"]
    check("cap_patterns_passed", r.outcome == Outcome.PASSED)
    check("cap_patterns_logged",
          bool(pcap) and pcap[0]["returned"] == 7
          and pcap[0]["kept"] == 5 and pcap[0]["dropped"] == 2)

    # (b) more than 20 candidates found -> capped at 20, excess recorded.
    with tempfile.TemporaryDirectory() as tmp:
        many = "".join(f"line CANDIDATE_MARKER {i}\n" for i in range(25))
        _init_repo(tmp, {"many.py": many})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": "CANDIDATE_MARKER", "rationale": "r"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["other.py"]))
        r = await _run_in_repo(tmp, orch)
    ccap = [e for e in r.debate_log if e["event"] == "closure_candidates_capped"]
    check("cap_candidates_passed", r.outcome == Outcome.PASSED)
    check("cap_candidates_count",
          r.closure_report is not None
          and len(r.closure_report.candidates) == 20)
    check("cap_candidates_logged",
          bool(ccap) and ccap[0]["found"] == 25
          and ccap[0]["kept"] == 20 and ccap[0]["dropped"] == 5)

    # 8. FINAL (step-9) PASSED PATH. Tests 1-7 all take the early-exit branch;
    # this one drives the full loop -- reviewer raises a BLOCKING issue, the
    # doer ACCEPTS it, fixes are applied, and the post-fix gate passes -- so the
    # run returns the FINAL PASSED. The sweep must attach there too.
    # (a) with a planted sibling -> report attached + closure_report event.
    blocking = {"issues": [{
        "id": "I1", "severity": "blocking",
        "issue": "unchecked subprocess result", "suggested_fix": "check it"}]}
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {
            "sibling.py": "def foo():\n    proc.communicate()  # STEP9_MARKER\n",
        })
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(
            cfg,
            {"bug_class": "unchecked subprocess result",
             "patterns": [{"regex": "STEP9_MARKER",
                           "rationale": "same unchecked-result shape"}]},
            review_reply=blocking)
        mdiff = MutatingDiff(["src/feature.py"])
        doer = AcceptingDoer(mdiff)
        orch = make(cfg, doer, reviewer, gate_pass, mdiff)
        r = await _run_in_repo(tmp, orch)
    events = [e["event"] for e in r.debate_log]
    check("step9_passed", r.outcome == Outcome.PASSED)
    # Confirm we took the FULL path, not the early-exit branch the other tests
    # ride: a real round ran, fixes were applied, and `passed` (step 9) fired.
    check("step9_not_early_exit", "early_exit" not in events)
    check("step9_passed_event", "passed" in events)
    check("step9_debated", r.rounds_used == 1)
    check("step9_fixes_applied", len(doer.apply_fixes_calls) == 1)
    check("step9_report_attached", r.closure_report is not None)
    s9cand = [(c.file, c.line) for c in (r.closure_report.candidates
                                         if r.closure_report else [])]
    check("step9_candidate_found", ("sibling.py", 2) in s9cand)
    check("step9_scan_called_once", reviewer.closure_calls == 1)
    s9_rep = [e for e in r.debate_log if e["event"] == "closure_report"]
    check("step9_report_event",
          len(s9_rep) == 1
          and s9_rep[0]["bug_class"] == "unchecked subprocess result"
          and s9_rep[0]["candidate_count"] == 1
          and any(c["file"] == "sibling.py" and c["line"] == 2
                  and c["pattern"] == "STEP9_MARKER"
                  for c in s9_rep[0]["candidates"]))

    # (b) same full path but a CLEAN sweep -> closure_clean logged on the final
    # PASSED path too, report left None (the step-6 convention holds here).
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(
            cfg, {"bug_class": "none", "patterns": []},
            review_reply=blocking)
        mdiff = MutatingDiff(["src/feature.py"])
        doer = AcceptingDoer(mdiff)
        orch = make(cfg, doer, reviewer, gate_pass, mdiff)
        r = await _run_in_repo(tmp, orch)
    events = [e["event"] for e in r.debate_log]
    clean_ev = [e for e in r.debate_log if e["event"] == "closure_clean"]
    check("step9_clean_passed", r.outcome == Outcome.PASSED)
    check("step9_clean_not_early_exit",
          "early_exit" not in events and "passed" in events)
    check("step9_clean_fixes_applied", len(doer.apply_fixes_calls) == 1)
    check("step9_clean_event_logged", len(clean_ev) == 1)
    check("step9_clean_report_none", r.closure_report is None)
    check("step9_clean_no_report_event",
          not any(e["event"] == "closure_report" for e in r.debate_log))

    # 9. LONG GREP LINE IS BOUNDED. A single real repo line 50k chars long that
    # a pattern matches must not ride into the report at full length -- the
    # candidate's `text` is capped at construction with a visible elision marker.
    # Pre-fix the line came through whole (grep output was attached verbatim).
    with tempfile.TemporaryDirectory() as tmp:
        long_line = "LONGLINE_MARKER " + "z" * 50000
        _init_repo(tmp, {"big.py": long_line + "\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [{"regex": "LONGLINE_MARKER", "rationale": "r"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["other.py"]))
        r = await _run_in_repo(tmp, orch)
    cands = r.closure_report.candidates if r.closure_report else []
    ctext = cands[0].text if cands else ""
    marker = f"... ({len(long_line)} chars total)"
    check("longline_passed", r.outcome == Outcome.PASSED)
    check("longline_one_candidate", len(cands) == 1)
    check("longline_bounded", len(ctext) <= _CLOSURE_MAX_TEXT + len(marker))
    check("longline_marked",
          ctext.startswith("LONGLINE_MARKER") and ctext.endswith(marker))

    # 10. LONG MODEL STRINGS BOUNDED. bug_class and each rationale are
    # single-line model explanations; both are capped in _parse_closure so
    # nothing unbounded reaches a ClosureReport. (A planted sibling gives the
    # report a candidate so it actually attaches.)
    big = "B" * 50000
    rat = "R" * 50000
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"sib.py": "MODELSTR_MARKER\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {
            "bug_class": big,
            "patterns": [{"regex": "MODELSTR_MARKER", "rationale": rat}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["other.py"]))
        r = await _run_in_repo(tmp, orch)
    rep = r.closure_report
    bc = rep.bug_class if rep else ""
    rr = rep.patterns[0].rationale if rep and rep.patterns else ""
    bc_marker = f"... ({len(big)} chars total)"
    rr_marker = f"... ({len(rat)} chars total)"
    check("modelstr_report_attached", rep is not None)
    check("modelstr_bugclass_bounded",
          len(bc) <= _CLOSURE_MAX_TEXT + len(bc_marker)
          and bc.endswith(bc_marker))
    check("modelstr_rationale_bounded",
          len(rr) <= _CLOSURE_MAX_TEXT + len(rr_marker)
          and rr.endswith(rr_marker))

    # 11. OVER-LONG REGEX IS REJECTED, NOT TRUNCATED. A regex past the length
    # limit must never run and must never appear in a truncated form -- a
    # shortened regex is a DIFFERENT regex that would silently change what was
    # searched. The distinctive start/end markers let us prove no truncated
    # fragment leaked: every occurrence of the start marker must be part of a
    # full-length regex occurrence. Crucially the regex is longer than _MAX_STR
    # (the _pack_event field cap), so if the raw regex were ever logged, its
    # start marker would survive in a _pack_event-truncated fragment while the
    # full regex would not -- the count assertion below would then fail. The
    # only way both counts stay equal is if the rejected regex is never logged
    # at all, which is exactly the contract.
    # A VALID sibling pattern matches too, so a ClosureReport is genuinely
    # emitted -- this is the case where the rejected regex could otherwise ride
    # into closure_report.patterns. It must be filtered out of the report, not
    # merely skipped in the grep loop.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "TRUNCMARK_ lives here\nSIBLING_MARKER\n"})
        cfg = HarnessConfig()
        long_regex = "TRUNCMARK_" + "a" * (_MAX_STR + 1000) + "_ENDMARK"
        reviewer = ClosureReviewer(cfg, {
            "bug_class": "c",
            "patterns": [
                {"regex": long_regex, "rationale": "r"},
                {"regex": "SIBLING_MARKER", "rationale": "sib"}]})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["other.py"]))
        r = await _run_in_repo(tmp, orch)
    blob = json.dumps(r.debate_log, default=str)
    if r.closure_report is not None:
        blob += r.closure_report.model_dump_json()
    pat_skips = [e for e in r.debate_log if e["event"] == "closure_skipped"
                 and e.get("scope") == "pattern"]
    report_regexes = ([p.regex for p in r.closure_report.patterns]
                      if r.closure_report else [])
    check("longregex_passed", r.outcome == Outcome.PASSED)
    # The valid sibling yields a candidate, so a report IS attached...
    check("longregex_report_attached", r.closure_report is not None)
    # ...but the over-long regex is filtered OUT of report.patterns entirely.
    check("longregex_absent_from_report", long_regex not in report_regexes)
    check("longregex_only_sibling_kept", report_regexes == ["SIBLING_MARKER"])
    check("longregex_skipped_logged",
          any(str(_CLOSURE_MAX_TEXT) in str(e.get("reason", ""))
              for e in pat_skips))
    # No truncated form of the regex appears: the elided form our helper would
    # produce is absent, and every start-marker occurrence is a full regex.
    check("longregex_no_elided_form",
          _bound_closure_text(long_regex) not in blob)
    check("longregex_never_truncated",
          blob.count("TRUNCMARK_") == blob.count(long_regex))

    # 12. SIGNAL DEATH IS NOT SUCCESS. A `git grep` killed by a signal returns a
    # NEGATIVE returncode (-15 SIGTERM, -9 SIGKILL), which is not > 1. If the
    # result handling only rejected `> 1`, it would fall through and parse the
    # killed process's PARTIAL stdout as a complete result -- fabricating
    # candidates from a dead grep. Stub run_subprocess_result to return -15 with
    # non-empty stdout and assert _git_grep SKIPS the pattern (logs
    # closure_skipped) and yields NO candidate from that partial output.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {"bug_class": "c", "patterns": []})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))

        async def _signal_killed(cmd, *, stdin_text=None):
            # Partial output a killed grep might have flushed before dying.
            return (-15, b"ghost.py:1:partial line from a killed grep\n", b"")

        saved = orch_mod.run_subprocess_result
        orch_mod.run_subprocess_result = _signal_killed
        try:
            prev = os.getcwd()
            os.chdir(tmp)
            try:
                grep_result = await orch._git_grep("ANYTHING")
            finally:
                os.chdir(prev)
        finally:
            orch_mod.run_subprocess_result = saved
    sig_skips = [e for e in orch.log if e["event"] == "closure_skipped"
                 and e.get("scope") == "pattern"]
    check("signal_death_no_candidate", grep_result is None)
    check("signal_death_skipped_logged", len(sig_skips) == 1)
    # The partial line must NOT have been parsed into a candidate anywhere.
    log_blob = json.dumps(orch.log, default=str)
    check("signal_death_partial_not_parsed", "ghost.py:1:partial" not in log_blob)

    # 13. CAPS BOUND THE WORK, not just the output. A reply with far more than 5
    # patterns must be capped BEFORE per-pattern validation: only the first 5 are
    # ever validated/run. We plant a MALFORMED pattern at index 5 -- if the cap
    # regressed to after-validation, model_validate would raise on it and the
    # whole reply would become closure_skipped(malformed); with the cap before
    # validation the malformed 6th is dropped untouched, parsing succeeds, and at
    # most 5 greps run. `returned` still reports the true count the model sent.
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        pats = [{"regex": f"NOMATCH_{i}", "rationale": "r"} for i in range(5)]
        pats.append({"not_a_regex": True})  # malformed: no regex/rationale
        pats += [{"regex": f"NOMATCH_tail_{i}", "rationale": "r"}
                 for i in range(10)]
        reviewer = ClosureReviewer(cfg, {"bug_class": "c", "patterns": pats})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        grep_calls = {"n": 0}
        orig_grep = orch._git_grep

        async def counting_grep(regex, _orig=orig_grep, _c=grep_calls):
            _c["n"] += 1
            return await _orig(regex)

        orch._git_grep = counting_grep
        r = await _run_in_repo(tmp, orch)
    pcap = [e for e in r.debate_log if e["event"] == "closure_patterns_capped"]
    malformed = [e for e in r.debate_log if e["event"] == "closure_skipped"
                 and e.get("reason") == "malformed"]
    check("cap_work_passed", r.outcome == Outcome.PASSED)
    # Cap applied before validation -> the malformed 6th was never validated.
    check("cap_work_not_malformed", malformed == [])
    check("cap_work_grep_bounded", grep_calls["n"] <= 5)
    check("cap_work_returned_true_count",
          bool(pcap) and pcap[0]["returned"] == len(pats)
          and pcap[0]["kept"] == 5
          and pcap[0]["dropped"] == len(pats) - 5)

    # 14. PER-FILE CAP. _git_grep must pass `-m <_CLOSURE_GREP_MAX_PER_FILE>` so
    # git itself caps matches PER FILE at the source -- one noisy file can't
    # dominate the whole candidate budget. A recording stub captures the argv;
    # assert `-m` and the constant are present and that the constant sits above
    # _CLOSURE_MAX_CANDIDATES (so a single file can't monopolize the budget).
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp, {"x.py": "hello\n"})
        cfg = HarnessConfig()
        reviewer = ClosureReviewer(cfg, {"bug_class": "c", "patterns": []})
        orch = make(cfg, StubDoer(), reviewer, gate_pass, diff_for(["x.py"]))
        recorded = {}

        async def _record(cmd, *, stdin_text=None):
            recorded["cmd"] = list(cmd)
            return (1, b"", b"")  # exit 1 = no matches (a success, empty result)

        saved = orch_mod.run_subprocess_result
        orch_mod.run_subprocess_result = _record
        try:
            await orch._git_grep("SOME_REGEX")
        finally:
            orch_mod.run_subprocess_result = saved
    argv = recorded.get("cmd", [])
    m_flag_ok = ("-m" in argv
                 and argv[argv.index("-m") + 1]
                 == str(orch._CLOSURE_GREP_MAX_PER_FILE))
    check("per_file_cap_m_flag_passed", m_flag_ok)
    check("per_file_cap_constant_above_candidates",
          orch._CLOSURE_GREP_MAX_PER_FILE > orch._CLOSURE_MAX_CANDIDATES)

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
