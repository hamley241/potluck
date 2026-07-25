"""Pre-dispatch bounded-call budget (hardening Step-0 §1.3, tripwire T4b).

The §1<->§2 coupling made concrete: the outer bound is outer_min = C_max x N_max,
and the `<= N_max` premise it rests on is ENFORCED pre-dispatch by
BudgetedStepRunner (caller-side; harness_core stays fenced), never assumed. Covers:

  T4b -- a run driven past N_max bounded calls fails closed
         (ESCALATED_CALL_BUDGET), REFUSED before the (N_max+1)-th call runs; and
         a real end-to-end run's actual bounded-call count is asserted <= N_max
         (so the pinned budget is real, and outer_min genuinely dominates).
  arith -- n_max / c_max / outer_min compose as the ratified formula, and the
         backoff knobs move C_max.
"""

import asyncio
import sys

from harness.config import HarnessConfig
from harness.schemas import DoerResponse, IssueResponse, Outcome
from harness.test_orchestrator import (
    StubDoer, StubReviewer, StubTiebreaker, make, gate_pass, diff_plain,
)


class RejectAllDoer(StubDoer):
    async def respond_to_review(self, spec, acceptance, diff, verdict, retry_note=None):
        return DoerResponse(responses=[
            IssueResponse(id=i.id, decision="reject", reasoning="stub")
            for i in verdict.issues])


def _heavy_run_cfg(**budget):
    cfg = HarnessConfig()
    cfg.debate.max_rounds = 2
    for k, v in budget.items():
        setattr(cfg.call_budget, k, v)
    # Keep the OUTER timeout from firing first (we want the call-budget path, not
    # ABORTED_BUDGET): set feature_seconds to the config's own computed bound.
    cfg.timeouts.feature_seconds = max(cfg.outer_min(), 10_000)
    return cfg


def _heavy_orch(cfg):
    issues = [{"id": f"C{i}", "severity": "blocking", "issue": f"iss {i}",
               "suggested_fix": "fix"} for i in range(3)]
    reviewer = StubReviewer(cfg, first={"issues": issues},
                            followups=[{"issues": issues}] * 3)
    tb = StubTiebreaker(cfg, {f"C{i}": "b" for i in range(3)})
    return make(cfg, RejectAllDoer({}), reviewer, tb, gate_pass, diff_plain)


async def main() -> bool:
    results = {}

    # --- T4b: a real heavy run stays <= N_max ---------------------------
    cfg = _heavy_run_cfg()                      # default pins: N_max = 30
    orch = _heavy_orch(cfg)
    r = await orch.run_feature("spec", "acc")
    n_max = cfg.n_max()
    results["heavy_run_within_budget"] = (orch.runner.calls_dispatched <= n_max)
    # It must NOT have been the budget that stopped it (proves 30 genuinely
    # dominates the ~11-call heavy path, not that it clipped it).
    results["heavy_run_not_budget_capped"] = (
        r.outcome != Outcome.ESCALATED_CALL_BUDGET)

    # --- T4b: driven PAST a tiny N_max -> fail closed, refused pre-dispatch
    tiny = _heavy_run_cfg(fixed_call_budget=2, per_round_call_budget=1)  # N_max = 4
    small_n = tiny.n_max()
    results["tiny_n_max_is_4"] = (small_n == 4)
    orch2 = _heavy_orch(tiny)
    r2 = await orch2.run_feature("spec", "acc")
    results["exceeds_budget_escalates"] = (
        r2.outcome == Outcome.ESCALATED_CALL_BUDGET)
    # Admission is BEFORE dispatch: EXACTLY N_max calls ran, then the next was
    # refused (never N_max+1 dispatched).
    results["refused_before_dispatch"] = (
        orch2.runner.calls_dispatched == small_n)
    # The escalation reason names the budget.
    results["escalation_names_budget"] = (
        r2.escalation_reason is not None
        and f"N_max={small_n}" in r2.escalation_reason)
    # A call_budget_exceeded event is logged.
    results["logs_budget_event"] = any(
        e.get("event") == "call_budget_exceeded" for e in r2.debate_log)

    # --- arithmetic: the ratified formula --------------------------------
    c = HarnessConfig()
    # C_max = heaviest base (600) * (1+R=1) + delay(5)*R = 1205 at defaults.
    results["c_max_default"] = (c.timeouts.c_max() == 600 * 2 + 5)
    # N_max = fixed(14) + max_rounds(2) * per_round(8) = 30.
    results["n_max_default"] = (c.n_max() == 14 + 2 * 8)
    results["outer_min_is_product"] = (
        c.outer_min() == c.timeouts.c_max() * c.n_max())
    # Backoff knobs move C_max: R=2, delay=10, base 600 -> 600*3 + 10*2 = 1820.
    c.timeouts.max_resume_retries = 2
    c.timeouts.backoff_delay_seconds = 10
    results["backoff_moves_c_max"] = (c.timeouts.c_max() == 600 * 3 + 10 * 2)
    # max_rounds moves N_max.
    c.debate.max_rounds = 3
    results["max_rounds_moves_n_max"] = (c.n_max() == 14 + 3 * 8)

    # --- validate refuses an under-outer_min feature_seconds -------------
    bad = HarnessConfig()
    bad.timeouts.feature_seconds = bad.outer_min() - 1
    refused = False
    try:
        bad.validate()
    except ValueError:
        refused = True
    results["under_outer_min_refuses"] = refused
    good = HarnessConfig()
    good.timeouts.feature_seconds = good.outer_min()
    ok = True
    try:
        good.validate()
    except ValueError:
        ok = False
    results["at_outer_min_ok"] = ok
    # codex round-9: raising max_rounds lifts N_max -> outer_min, so a high
    # max_rounds against the default feature_seconds must be REFUSED (the CLI's
    # --max-rounds path re-validates unconditionally to enforce this).
    mr = HarnessConfig()
    mr.debate.max_rounds = 100  # N_max = 14 + 100*8 = 814 -> outer_min >> default
    mr_refused = False
    try:
        mr.validate()
    except ValueError:
        mr_refused = True
    results["high_max_rounds_refuses"] = mr_refused
    # feature_seconds=0 is refused (no fire-immediately escape, round-9).
    z = HarnessConfig()
    z.timeouts.feature_seconds = 0
    z_ref = False
    try:
        z.validate()
    except ValueError:
        z_ref = True
    results["zero_feature_seconds_refused"] = z_ref
    # codex round-10 MINOR: a NEGATIVE max_rounds makes N_max/outer_min negative,
    # which would sail past `feature_seconds >= outer_min`. Refuse at source.
    neg_mr = HarnessConfig()
    neg_mr.debate.max_rounds = -100
    nmr_ref = False
    try:
        neg_mr.validate()
    except ValueError:
        nmr_ref = True
    results["negative_max_rounds_refuses"] = nmr_ref
    # codex round-10 MAJOR: a NEGATIVE calls_dispatched in a checkpoint would let a
    # resumed run dispatch N_max+1 calls -- refused at PARSE (schema ge=0).
    from harness.schemas import (
        RunCheckpoint, ExecCursor, Phase, CheckpointIdentity, TreeSnapshot,
        CHECKPOINT_SCHEMA_VERSION,
    )
    _ident = CheckpointIdentity(
        schema_version=CHECKPOINT_SCHEMA_VERSION, criteria_content_hash="c",
        input_document_digest="d", effective_config_digest="e",
        run_manifest_id="m", repo_base_commit="base", tree_snapshot_digest="td")
    _tree = TreeSnapshot(repo_base_commit="base", work_tree_oid="w",
                         index_tree_oid="i", tree_snapshot_digest="td")
    parse_refused = False
    try:
        RunCheckpoint(cursor=ExecCursor(phase=Phase.IMPLEMENT), rounds_used=0,
                      identity=_ident, tree=_tree, calls_dispatched=-1)
    except Exception:
        parse_refused = True
    results["negative_calls_dispatched_refused_at_parse"] = parse_refused

    # A non-positive fixed budget and a negative per-round budget refuse.
    for fld, val in [("fixed_call_budget", 0), ("per_round_call_budget", -1)]:
        c2 = HarnessConfig()
        setattr(c2.call_budget, fld, val)
        c2.timeouts.feature_seconds = 10_000_000
        r_ref = False
        try:
            c2.validate()
        except ValueError:
            r_ref = True
        results[f"bad_{fld}_refuses"] = r_ref

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
