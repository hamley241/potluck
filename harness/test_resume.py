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


class CountFailDoer(DoerClient):
    """Times out `fail_times` times on implement, then succeeds -- to exercise
    §2.4 backoff retries on the resumed failed step."""
    def __init__(self, fail_times, calls):
        self.fail_times = fail_times
        self.calls = calls
    async def implement(self, spec, acceptance):
        self.calls.append("implement")
        if len(self.calls) <= self.fail_times:
            raise TimeoutEscalation("doer_no_signal", "backoff test timeout")
        return "implemented"
    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        self.calls.append("respond")
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision="reject", reasoning="s")
            for i in verdict.issues])
    async def apply_fixes(self, issues, diff):
        return "applied"


async def _backoff(fail_times):
    """Resume at IMPLEMENT with a doer that times out `fail_times` times; §2.4
    retries at the SAME timeout, bounded by MAX_RESUME_RETRIES (=2 here, so 3
    attempts). Returns (outcome, implement_call_count)."""
    cfg = HarnessConfig()
    cfg.debate.max_rounds = 1
    cfg.timeouts.max_resume_retries = 2
    cfg.timeouts.backoff_delay_seconds = 0     # keep the test fast
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    store = FakeStore()
    blocking = {"issues": [{"id": "C1", "severity": "blocking",
                            "issue": "iss", "suggested_fix": "fix"}]}

    async def gate_ok():
        import json
        return json.dumps({"ok": True, "output": "green", "exit_code": 0})

    async def diff():
        return "diff\n+x\n"

    # pass 1: create a checkpoint at IMPLEMENT (fail there once).
    p1 = CountFailDoer(fail_times=1, calls=[])
    rev1 = RecReviewer(cfg, blocking, [])
    o1 = Orchestrator(cfg, p1, rev1, RecTiebreaker(cfg, {}, []), gate_ok, diff,
                      ab_swap=lambda _id: False, checkpointer=_checkpointer(store))
    await o1.run_feature("s", "a")
    resume_ckpt = _checkpointer(store).load_and_verify()

    # resume: the doer times out `fail_times` times before succeeding.
    calls = []
    doer = CountFailDoer(fail_times=fail_times, calls=calls)
    rev = RecReviewer(cfg, blocking, [])
    o2 = Orchestrator(cfg, doer, rev, RecTiebreaker(cfg, {"C1": "b"}, []),
                      gate_ok, diff, ab_swap=lambda _id: False,
                      checkpointer=_checkpointer(store))
    r = await o2.run_feature("s", "a", resume=resume_ckpt)
    return r.outcome, calls.count("implement")


async def main() -> bool:
    results = {}

    # (f) §2.4 backoff: within MAX_RESUME_RETRIES the resumed step retries and
    #     recovers; beyond it, re-escalates ESCALATED_TIMEOUT (still resumable).
    oc_recover, n_recover = await _backoff(fail_times=2)   # 2 fails, 3rd ok
    results["backoff_retries_within_cap"] = (n_recover == 3)
    results["backoff_recovers"] = (oc_recover != Outcome.ESCALATED_TIMEOUT)
    oc_exhaust, n_exhaust = await _backoff(fail_times=5)   # exceeds cap
    results["backoff_exhausts_at_cap"] = (n_exhaust == 3)  # 1 + 2 retries
    results["backoff_reescalates_timeout"] = (oc_exhaust == Outcome.ESCALATED_TIMEOUT)

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

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
