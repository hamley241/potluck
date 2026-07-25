"""Resumable timed-out loops -- re-entry (hardening Step-0 §2, tripwire T2).

T2: resume NEVER re-litigates completed phases AND resumes the EXACT failed call.
A timeout is injected at each cursor phase (potluck: IMPLEMENT, ROUND_RESPOND,
ROUND_FOLLOWUP, APPLY_FIXES -- incl. the potluck-only generative doer phases);
the resumed run must re-enter exactly there, re-running nothing prior.

This drives the re-entry logic through a FakeCheckpointStore (no git), isolating
cursor/state dispatch from the git-tree engine -- which is proven independently in
test_checkpoint (T3b). A timeout is simulated by raising TimeoutEscalation from the
injected step (deterministic; no real wall-clock sleeping), which propagates to
run_feature exactly as a real StepRunner timeout does.
"""

import asyncio
import sys

from harness_core.runner import TimeoutEscalation
from harness.config import HarnessConfig
from harness.schemas import (
    DoerResponse, IssueResponse, Outcome, Phase, TreeSnapshot,
)
from harness.orchestrator import Orchestrator, DoerClient, ReviewerClient, TiebreakerClient
from harness.checkpoint import Checkpointer


class FakeStore:
    """In-memory checkpoint store: records writes, fakes tree snapshot/restore.
    load() returns the LAST checkpoint written -- which, when a call times out
    AFTER its checkpoint-before-write, is the cursor for that failed call."""
    def __init__(self):
        self.written = []
        self.restored = None
        self._n = 0

    def snapshot(self):
        self._n += 1
        return TreeSnapshot(repo_base_commit="base", work_tree_oid=f"w{self._n}",
                            index_tree_oid=f"i{self._n}",
                            tree_snapshot_digest=f"d{self._n}")

    def restore(self, tree):
        self.restored = tree

    def write(self, ckpt):
        self.written.append(ckpt)

    def load(self):
        if not self.written:
            from harness.checkpoint import CheckpointError
            raise CheckpointError("no checkpoint")
        return self.written[-1]


def _checkpointer(store):
    # Same identity inputs on both passes so load_and_verify's fail-closed
    # recompute PASSES (the divergence path is covered in test_checkpoint T3).
    return Checkpointer(store, spec="s", acceptance="a",
                        config_digest_value="cfg", run_manifest_id="m")


class DiffState:
    """Shared mutable diff so apply_fixes produces an OBSERVABLE change (Member B
    requires post-apply != pre-apply). The doer appends on a successful
    apply_fixes; _diff() reads the current text."""
    def __init__(self):
        self.text = "diff --git a/f b/f\n@@ -1 +1 @@\n-x\n+y\n"


class RecDoer(DoerClient):
    def __init__(self, decisions, calls, diff_state, fail_at=None):
        self.decisions = decisions
        self.calls = calls
        self.diff_state = diff_state
        self.fail_at = fail_at   # method name to time out on, once
        self._fired = False

    def _maybe_fail(self, name):
        if self.fail_at == name and not self._fired:
            self._fired = True
            raise TimeoutEscalation("doer_no_signal", f"injected timeout at {name}")

    async def implement(self, spec, acceptance):
        self.calls.append("implement")
        self._maybe_fail("implement")
        return "implemented"

    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        self.calls.append("respond")
        self._maybe_fail("respond")
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision=self.decisions.get(i.id, "accept"),
                          reasoning="stub") for i in verdict.issues])

    async def apply_fixes(self, issues, diff):
        self.calls.append("apply_fixes")
        self._maybe_fail("apply_fixes")   # timeout -> no mutation
        self.diff_state.text += "\n@@ apply @@\n+fixed\n"   # observable change
        return "applied"


class RecReviewer(ReviewerClient):
    def __init__(self, cfg, first, calls, followups=None, fail_at=None):
        super().__init__(cfg)
        self.first = first
        self.followups = followups or []
        self.calls = calls
        self.fail_at = fail_at
        self._fired = False
        self._n = 0

    def _maybe_fail(self, name):
        if self.fail_at == name and not self._fired:
            self._fired = True
            raise TimeoutEscalation("reviewer_no_signal", f"injected timeout at {name}")

    async def review(self, spec, acceptance, diff, retry_note=None):
        self.calls.append("review")
        self._maybe_fail("review")
        import json
        return json.dumps(self.first)

    async def respond(self, spec, acceptance, diff, rejections, retry_note=None):
        self.calls.append("followup")
        self._maybe_fail("followup")
        import json
        out = self.followups[self._n] if self._n < len(self.followups) else {"issues": []}
        self._n += 1
        return json.dumps(out)

    async def closure_scan(self, spec, diff, retry_note=None):
        import json
        return json.dumps({"bug_class": "none", "patterns": []})


class RecTiebreaker(TiebreakerClient):
    def __init__(self, cfg, sides, calls, fail_at=None):
        super().__init__(cfg)
        self.sides = sides
        self.calls = calls
        self.fail_at = fail_at
        self._fired = False

    async def adjudicate(self, spec, acceptance, diff, issue_id, a, b, retry_note=None):
        self.calls.append("tiebreak")
        if self.fail_at == "tiebreak" and not self._fired:
            self._fired = True
            raise TimeoutEscalation("tiebreak_no_signal", "injected timeout at tiebreak")
        import json
        return json.dumps({"id": issue_id, "sides_with": self.sides.get(issue_id, "b"),
                           "reasoning": "stub"})


async def _gate_ok():
    import json
    return json.dumps({"ok": True, "output": "green", "exit_code": 0})


def _build(cfg, store, decisions, first, calls, diff_state, followups=None,
           doer_fail=None, rev_fail=None, tb_fail=None, sides=None):
    doer = RecDoer(decisions, calls, diff_state, fail_at=doer_fail)
    reviewer = RecReviewer(cfg, first, calls, followups=followups, fail_at=rev_fail)
    tb = RecTiebreaker(cfg, sides or {}, calls, fail_at=tb_fail)

    async def _diff():
        return diff_state.text

    return Orchestrator(cfg, doer, reviewer, tb, _gate_ok, _diff,
                        ab_swap=lambda _id: False, checkpointer=_checkpointer(store))


async def _fail_then_resume(target_method, target_phase, decisions, first,
                            followups=None, sides=None, max_rounds=1):
    """Run once with a timeout injected at target_method; assert it checkpointed
    the target phase. Then resume and assert re-entry at target_phase with no
    prior phase re-run."""
    cfg = HarnessConfig()
    cfg.debate.max_rounds = max_rounds
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    diff_state = DiffState()  # shared: pass-1 timeout precedes any mutation

    # --- pass 1: inject the timeout ---
    first_calls = []
    kwargs = dict(decisions=decisions, first=first, calls=first_calls,
                  diff_state=diff_state, followups=followups, sides=sides)
    for who, meth in (("doer_fail", {"implement", "respond", "apply_fixes"}),
                      ("rev_fail", {"review", "followup"}),
                      ("tb_fail", {"tiebreak"})):
        if target_method in meth:
            kwargs[who] = target_method
    orch = _build(cfg, store, **kwargs)
    r1 = await orch.run_feature("s", "a")

    checkpointed = (store.written and store.written[-1].cursor.phase == target_phase)

    # --- pass 2: resume (no injected failure), fresh recorders ---
    resume_ckpt = _checkpointer(store).load_and_verify()
    resume_calls = []
    orch2 = _build(cfg, store, decisions=decisions, first=first,
                   calls=resume_calls, diff_state=diff_state,
                   followups=followups, sides=sides)
    # restore the tree (no-op FakeStore) then re-enter at the cursor
    orch2._checkpointer.restore(resume_ckpt.tree)
    r2 = await orch2.run_feature("s", "a", resume=resume_ckpt)

    return {
        "escalated_timeout": r1.outcome == Outcome.ESCALATED_TIMEOUT,
        "checkpointed_target": checkpointed,
        "resume_calls": resume_calls,
        "outcome2": r2.outcome,
    }


# Which methods are PRIOR to each target (must NOT be re-run on resume).
_PRIOR = {
    "implement": set(),                                   # first phase
    "respond": {"implement", "review"},
    "followup": {"implement", "review"},                 # respond is same-round, re-run? no: skipped
    "apply_fixes": {"implement", "review", "respond"},
}


async def _git_integration() -> dict:
    """The seam: a full orchestrator resume over the REAL git-backed store
    (GitCheckpointStore + snapshot/restore over an actual temp git repo). A
    timeout at APPLY_FIXES leaves a real checkpoint + tree; resume restores the
    tree losslessly and re-enters, and the resumed apply_fixes edit lands."""
    import os
    import subprocess
    import tempfile
    from harness.checkpoint import GitCheckpointStore, Checkpointer

    def git(args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    out = {}
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path as P
        repo = P(d) / "repo"
        repo.mkdir()
        git(["init", "-q", "-b", "main"], repo)
        git(["config", "user.email", "t@t"], repo)
        git(["config", "user.name", "t"], repo)
        (repo / "seed.txt").write_text("seed\n")
        git(["add", "-A"], repo)
        git(["commit", "-qm", "base"], repo)

        class GitDoer(DoerClient):
            def __init__(self, calls, fail=False):
                self.calls = calls
                self.fail = fail
            async def implement(self, spec, acceptance):
                self.calls.append("implement")
                (repo / "work.txt").write_text("implemented\n")
                return "done"
            async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
                self.calls.append("respond")
                return DoerResponse(responses=[
                    IssueResponse(id=i.id, decision="accept", reasoning="s")
                    for i in verdict.issues])
            async def apply_fixes(self, issues, diff):
                self.calls.append("apply_fixes")
                if self.fail:
                    raise TimeoutEscalation("doer_no_signal", "injected at apply_fixes")
                (repo / "work.txt").write_text("implemented\nfixed\n")
                return "applied"

        blocking = {"issues": [{"id": "C1", "severity": "blocking",
                                "issue": "iss", "suggested_fix": "fix"}]}

        async def gate_ok():
            import json
            return json.dumps({"ok": True, "output": "green", "exit_code": 0})

        async def get_diff():
            f = repo / "work.txt"
            return f.read_text() if f.exists() else ""

        cfg = HarnessConfig()
        cfg.debate.max_rounds = 1
        cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)

        run_dir = P(d) / "runs"   # OUTSIDE the repo tree, so clean -fdx can't nuke it

        def mk(calls, fail):
            store = GitCheckpointStore(run_dir, repo=repo)
            cp = Checkpointer(store, "s", "a", "cfg", "m")
            doer = GitDoer(calls, fail=fail)
            rev = RecReviewer(cfg, blocking, calls)
            tb = RecTiebreaker(cfg, {}, calls)
            orch = Orchestrator(cfg, doer, rev, tb, gate_ok, get_diff,
                                ab_swap=lambda _id: False, checkpointer=cp)
            return orch, cp

        # pass 1: fail at apply_fixes
        c1 = []
        orch1, _ = mk(c1, fail=True)
        r1 = await orch1.run_feature("s", "a")
        out["timed_out"] = (r1.outcome == Outcome.ESCALATED_TIMEOUT)

        # A real checkpoint sidecar exists, cursor at APPLY_FIXES.
        loaded = GitCheckpointStore(run_dir, repo=repo).load()
        out["ckpt_at_apply_fixes"] = (loaded.cursor.phase == Phase.APPLY_FIXES)
        out["ckpt_has_tree"] = (loaded.tree is not None
                                and loaded.tree.work_tree_oid != "")

        # Operator cleans the dead attempt's tree back to base (restore's
        # precondition), then we resume.
        git(["clean", "-fdxq"], repo)
        c2 = []
        orch2, cp2 = mk(c2, fail=False)
        resume_ckpt = cp2.load_and_verify()
        cp2.restore(resume_ckpt.tree)
        # The restore reestablished the pre-apply tree exactly (work.txt back).
        out["tree_restored"] = ((repo / "work.txt").read_text() == "implemented\n")
        r2 = await orch2.run_feature("s", "a", resume=resume_ckpt)
        out["resumed_pass"] = (r2.outcome == Outcome.PASSED)
        out["reentered_apply_fixes"] = ("apply_fixes" in c2)
        out["skipped_prior_on_resume"] = not ({"implement", "respond"} & set(c2))
        # The resumed apply_fixes edit actually landed.
        out["apply_fixes_edit_landed"] = (
            (repo / "work.txt").read_text() == "implemented\nfixed\n")
    return out


class _CtrDoer(DoerClient):
    """Fails on implement until the SHARED counter reaches `succeed_on` total
    implement calls, then succeeds -- to exercise the one-attempt-per-resume §2.4
    model (each resume makes exactly ONE attempt; the cumulative cap is 1+R)."""
    def __init__(self, ctr, succeed_on):
        self.ctr = ctr
        self.succeed_on = succeed_on
    async def implement(self, spec, acceptance):
        self.ctr["n"] += 1
        if self.ctr["n"] < self.succeed_on:
            raise TimeoutEscalation("doer_no_signal", "backoff test timeout")
        return "implemented"
    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision="accept", reasoning="s")
            for i in verdict.issues])
    async def apply_fixes(self, issues, diff):
        return "applied"


async def _backoff(succeed_on):
    """Drive the fresh run + a resume LOOP (one implement attempt each, R=2 so the
    cumulative cap is 1+R=3). `succeed_on` = the total implement-call index the
    doer finally succeeds on (>3 => never, to test exhaustion). Returns
    (outcome, total_implement_attempts, resumes_that_dispatched)."""
    cfg = HarnessConfig()
    cfg.debate.max_rounds = 1
    cfg.timeouts.max_resume_retries = 2
    cfg.timeouts.backoff_delay_seconds = 0     # keep the test fast
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    ctr = {"n": 0}

    async def gate_ok():
        import json
        return json.dumps({"ok": True, "output": "green", "exit_code": 0})

    async def diff():
        return "diff\n+x\n"

    def mk():
        return Orchestrator(cfg, _CtrDoer(ctr, succeed_on),
                            RecReviewer(cfg, {"issues": []}, []),
                            RecTiebreaker(cfg, {}, []), gate_ok, diff,
                            ab_swap=lambda _id: False,
                            checkpointer=_checkpointer(store))

    # fresh run: implement attempt #1 (fails -> checkpoint at IMPLEMENT, attempt 0).
    outcome = (await mk().run_feature("s", "a")).outcome
    resumes = 0
    for _ in range(6):  # guard against non-termination
        if outcome != Outcome.ESCALATED_TIMEOUT:
            break
        before = ctr["n"]
        ck = _checkpointer(store).load_and_verify()
        outcome = (await mk().run_feature("s", "a", resume=ck)).outcome
        if ctr["n"] > before:
            resumes += 1        # this resume actually dispatched an attempt
    return outcome, ctr["n"], resumes


async def main() -> bool:
    results = {}

    # (f) §2.4 backoff, one-attempt-per-resume (codex round-1 BLOCKING fix): the
    #     failed step gets 1+R TOTAL attempts across original+resumes, never
    #     1+R per resume. R=2 -> cap 3 total implement attempts.
    #  recover: doer succeeds on the 3rd total attempt (original + 2 resumes).
    oc_recover, n_recover, res_recover = await _backoff(succeed_on=3)
    results["backoff_total_attempts_capped"] = (n_recover == 3)
    results["backoff_recovers_at_cap"] = (oc_recover == Outcome.PASSED)
    results["backoff_two_resumes_dispatched"] = (res_recover == 2)
    #  exhaust: doer never succeeds -> after R resume attempts the next resume is
    #  refused (attempt >= R) WITHOUT dispatching, total attempts stays at 1+R.
    oc_exhaust, n_exhaust, res_exhaust = await _backoff(succeed_on=99)
    results["backoff_exhausts_at_cap"] = (n_exhaust == 3)          # 1 original + 2 resume
    results["backoff_reescalates_timeout"] = (oc_exhaust == Outcome.ESCALATED_TIMEOUT)
    results["backoff_no_dispatch_past_cap"] = (res_exhaust == 2)   # 3rd resume dispatched nothing

    # (g) MAJOR-5 (codex round-1): the bounded-call budget is CUMULATIVE across
    #     resumes -- calls_dispatched is checkpointed and restored, so repeated
    #     resumes cannot each admit a fresh N_max.
    cfg = HarnessConfig()
    cfg.debate.max_rounds = 1
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    diff_state = DiffState()
    blocking = {"issues": [{"id": "C1", "severity": "blocking",
                            "issue": "iss", "suggested_fix": "fix"}]}
    # Fail at APPLY_FIXES so several calls are dispatched before the checkpoint.
    o = _build(cfg, store, decisions={"C1": "accept"}, first=blocking,
               calls=[], diff_state=diff_state, doer_fail="apply_fixes")
    await o.run_feature("s", "a")
    ck = store.written[-1]
    results["budget_checkpointed_nonzero"] = (ck.calls_dispatched > 0)
    # Resume: the fresh runner is seeded from the checkpointed cumulative count,
    # not reset to 0.
    o2 = _build(cfg, store, decisions={"C1": "accept"}, first=blocking,
                calls=[], diff_state=diff_state)
    o2._checkpointer.restore(ck.tree)
    # Before running, restore_count hasn't fired yet (it fires inside _run); drive
    # the resume and assert the final cumulative count exceeds a single phase's
    # worth -- i.e. the pre-checkpoint calls were carried, not dropped.
    await o2.run_feature("s", "a", resume=ck)
    results["budget_cumulative_across_resume"] = (
        o2.runner.calls_dispatched > ck.calls_dispatched)
    # codex round-3 MAJOR: after a timeout, the checkpoint's count is BUMPED to
    # the ACTUAL dispatch count -- it equals the runner's count (the timed-out
    # dispatch IS counted), not the pre-dispatch value (under-count, round-2) and
    # not a blind +1 (over-count, round-3). o's apply_fixes timed out.
    results["budget_bumped_to_actual_on_timeout"] = (
        ck.calls_dispatched == o.runner.calls_dispatched)

    # (h) BLOCKING-2 (codex round-1): "before AND after" checkpoints. A crash
    #     AFTER a call succeeds (a DONE write, completed=True) must resume PAST
    #     the completed call, never re-running it / discarding its tree work.
    cfg = HarnessConfig()
    cfg.debate.max_rounds = 1
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    ds = DiffState()
    blk = {"issues": [{"id": "C1", "severity": "blocking",
                       "issue": "iss", "suggested_fix": "fix"}]}
    o = _build(cfg, store, decisions={"C1": "accept"}, first=blk,
               calls=[], diff_state=ds)     # full accept-path run -> PASSED
    r = await o.run_feature("s", "a")
    results["blocking2_run_passed"] = (r.outcome == Outcome.PASSED)
    # Find the DONE write for APPLY_FIXES (completed=True) -- the "crash after
    # apply_fixes succeeded" checkpoint.
    done_apply = [w for w in store.written
                  if w.cursor.phase == Phase.APPLY_FIXES and w.cursor.completed]
    results["blocking2_apply_done_written"] = (len(done_apply) == 1)
    # Resume from it: apply_fixes must NOT re-run (it completed); the run
    # finishes from GATE_FINAL onward.
    resume_calls = []
    o3 = _build(cfg, store, decisions={"C1": "accept"}, first=blk,
                calls=resume_calls, diff_state=ds)
    r3 = await o3.run_feature("s", "a", resume=done_apply[0])
    results["blocking2_skips_completed_apply"] = ("apply_fixes" not in resume_calls)
    results["blocking2_resumes_to_terminal"] = (r3.outcome == Outcome.PASSED)

    # (i) MAJOR-2 (codex round-1): per-concern tiebreak idempotence. Two concerns
    #     go to tiebreak; the 2nd times out. Resume must re-adjudicate ONLY the
    #     2nd -- the 1st (already decided) is skipped, not re-adjudicated.
    class TBRec(RecTiebreaker):
        def __init__(self, cfg, fail_on):
            super().__init__(cfg, {}, [])
            self.asked = []
            self.fail_on = fail_on
            self._fired = set()
        async def adjudicate(self, spec, acceptance, diff, issue_id, a, b, retry_note=None):
            self.asked.append(issue_id)
            if issue_id == self.fail_on and issue_id not in self._fired:
                self._fired.add(issue_id)
                raise TimeoutEscalation("tiebreaker_no_signal", "inject")
            import json
            return json.dumps({"id": issue_id, "sides_with": "b", "reasoning": "s"})

    cfg = HarnessConfig()
    cfg.debate.max_rounds = 1
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    two = {"issues": [
        {"id": "C1", "severity": "blocking", "issue": "i1", "suggested_fix": "f"},
        {"id": "C2", "severity": "blocking", "issue": "i2", "suggested_fix": "f"}]}

    async def gate_ok2():
        import json
        return json.dumps({"ok": True, "output": "green", "exit_code": 0})

    async def diff2():
        return "diff\n+x\n"

    # pass 1: doer rejects both, reviewer holds both -> tiebreak; C2 times out.
    doer1 = RecDoer({"C1": "reject", "C2": "reject"}, [], DiffState())
    rev1 = RecReviewer(cfg, two, [], followups=[two])
    tb1 = TBRec(cfg, fail_on="C2")
    o = Orchestrator(cfg, doer1, rev1, tb1, gate_ok2, diff2,
                     ab_swap=lambda _id: False, checkpointer=_checkpointer(store))
    r = await o.run_feature("s", "a")
    results["major2_pass1_timeout"] = (r.outcome == Outcome.ESCALATED_TIMEOUT)
    results["major2_pass1_adjudicated_both_attempted"] = (tb1.asked == ["C1", "C2"])
    # resume: only C2 re-adjudicated (C1 decided, skipped).
    ck = _checkpointer(store).load_and_verify()
    doer2 = RecDoer({"C1": "reject", "C2": "reject"}, [], DiffState())
    rev2 = RecReviewer(cfg, two, [], followups=[two])
    tb2 = TBRec(cfg, fail_on=None)
    o2 = Orchestrator(cfg, doer2, rev2, tb2, gate_ok2, diff2,
                      ab_swap=lambda _id: False, checkpointer=_checkpointer(store))
    await o2.run_feature("s", "a", resume=ck)
    results["major2_resume_only_readjudicates_C2"] = (tb2.asked == ["C2"])
    results["major2_resume_skips_decided_C1"] = ("C1" not in tb2.asked)
    # codex round-2 BLOCKING: resume from C1's POST write (crash after C1 decided,
    # BEFORE C2's head write) must still skip C1 -- the per-concern post write
    # persisted C1's decision, so it is not re-adjudicated / discarded.
    c1_post = [w for w in store.written
               if w.cursor.phase == Phase.TIEBREAK
               and not w.cursor.completed
               and w.cursor.failed_step is None
               and w.state.get("tiebreak_decided", {}).get("C1")
               and "C2" not in w.state.get("tiebreak_decided", {})]
    results["major2_c1_post_write_exists"] = (len(c1_post) >= 1)
    tb3 = TBRec(cfg, fail_on=None)
    o3 = Orchestrator(cfg, RecDoer({"C1": "reject", "C2": "reject"}, [], DiffState()),
                      RecReviewer(cfg, two, [], followups=[two]), tb3, gate_ok2,
                      diff2, ab_swap=lambda _id: False,
                      checkpointer=_checkpointer(store))
    await o3.run_feature("s", "a", resume=c1_post[0])
    results["major2_resume_from_post_skips_C1"] = (tb3.asked == ["C2"])

    # (e) integration: real git-backed store through a full resume.
    g = await _git_integration()
    results["git_timed_out"] = g["timed_out"]
    results["git_ckpt_at_apply_fixes"] = g["ckpt_at_apply_fixes"]
    results["git_ckpt_has_tree"] = g["ckpt_has_tree"]
    results["git_tree_restored"] = g["tree_restored"]
    results["git_resumed_pass"] = g["resumed_pass"]
    results["git_reentered_apply_fixes"] = g["reentered_apply_fixes"]
    results["git_skipped_prior"] = g["skipped_prior_on_resume"]
    results["git_apply_fixes_edit_landed"] = g["apply_fixes_edit_landed"]

    # Reviewer raises one blocking issue.
    blocking = {"issues": [{"id": "C1", "severity": "blocking",
                            "issue": "iss", "suggested_fix": "fix"}]}

    # (a) IMPLEMENT (potluck-only generative doer phase): accept path.
    a = await _fail_then_resume("implement", Phase.IMPLEMENT,
                                decisions={"C1": "accept"}, first=blocking)
    results["implement_pass1_timeout"] = a["escalated_timeout"]
    results["implement_checkpointed"] = a["checkpointed_target"]
    results["implement_reenters"] = ("implement" in a["resume_calls"])
    results["implement_resumes_to_pass"] = (a["outcome2"] == Outcome.PASSED)

    # (b) ROUND_RESPOND: accept path; fail at the doer's respond.
    b = await _fail_then_resume("respond", Phase.ROUND_RESPOND,
                                decisions={"C1": "accept"}, first=blocking)
    results["respond_pass1_timeout"] = b["escalated_timeout"]
    results["respond_checkpointed"] = b["checkpointed_target"]
    results["respond_reenters"] = ("respond" in b["resume_calls"])
    results["respond_skips_prior"] = not (_PRIOR["respond"] & set(b["resume_calls"]))
    results["respond_resumes_to_pass"] = (b["outcome2"] == Outcome.PASSED)

    # (c) ROUND_FOLLOWUP: reject path so a followup happens; fail at followup.
    #     tiebreaker sides with reviewer (b) so the resumed run terminates.
    c = await _fail_then_resume("followup", Phase.ROUND_FOLLOWUP,
                                decisions={"C1": "reject"}, first=blocking,
                                followups=[blocking], sides={"C1": "b"})
    results["followup_pass1_timeout"] = c["escalated_timeout"]
    results["followup_checkpointed"] = c["checkpointed_target"]
    results["followup_reenters"] = ("followup" in c["resume_calls"])
    results["followup_skips_prior"] = not (
        {"implement", "review", "respond"} & set(c["resume_calls"]))

    # (d) APPLY_FIXES (potluck-only generative doer phase): accept path; fail at
    #     apply_fixes; resume must NOT re-run implement/review/respond.
    d = await _fail_then_resume("apply_fixes", Phase.APPLY_FIXES,
                                decisions={"C1": "accept"}, first=blocking)
    results["apply_fixes_pass1_timeout"] = d["escalated_timeout"]
    results["apply_fixes_checkpointed"] = d["checkpointed_target"]
    results["apply_fixes_reenters"] = ("apply_fixes" in d["resume_calls"])
    results["apply_fixes_skips_prior"] = not (
        _PRIOR["apply_fixes"] & set(d["resume_calls"]))
    results["apply_fixes_resumes_to_pass"] = (d["outcome2"] == Outcome.PASSED)

    # ===== codex round-10 BLOCKING: identity must be BOUND to the tree ==========
    # load_and_verify recomputes the identity from ckpt.TREE's fields, so a tamper
    # that swaps ckpt.tree for another internally-valid snapshot (leaving identity
    # untouched) is REFUSED -- restore never trusts an unbound tree.
    from harness.schemas import (
        RunCheckpoint, ExecCursor, CheckpointIdentity, CHECKPOINT_SCHEMA_VERSION,
    )
    from harness.checkpoint import build_identity, CheckpointError

    def _refuses_cp(fn):
        try:
            fn()
            return False
        except CheckpointError:
            return True

    def _mk(base, digest, ident_base, ident_digest):
        ident = build_identity("s", "a", "cfg", ident_base, ident_digest, "m")
        return RunCheckpoint(
            cursor=ExecCursor(phase=Phase.IMPLEMENT), rounds_used=0, identity=ident,
            tree=TreeSnapshot(repo_base_commit=base, work_tree_oid="w",
                              index_tree_oid="i", tree_snapshot_digest=digest))

    class _OneStore:
        def __init__(self, ckpt): self._c = ckpt
        def load(self): return self._c

    # Matched: identity's tree fields == ckpt.tree's -> verifies.
    good = _mk("baseX", "digX", "baseX", "digX")
    ok_bound = True
    try:
        Checkpointer(_OneStore(good), "s", "a", "cfg", "m").load_and_verify()
    except CheckpointError:
        ok_bound = False
    results["tree_bound_identity_verifies"] = ok_bound
    # Tampered tree.repo_base_commit (identity still says baseX) -> REFUSED.
    tamper_base = _mk("baseEVIL", "digX", "baseX", "digX")
    results["tampered_tree_base_refuses"] = _refuses_cp(
        lambda: Checkpointer(_OneStore(tamper_base), "s", "a", "cfg", "m").load_and_verify())
    # Tampered tree.tree_snapshot_digest -> REFUSED.
    tamper_dig = _mk("baseX", "digEVIL", "baseX", "digX")
    results["tampered_tree_digest_refuses"] = _refuses_cp(
        lambda: Checkpointer(_OneStore(tamper_dig), "s", "a", "cfg", "m").load_and_verify())

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
