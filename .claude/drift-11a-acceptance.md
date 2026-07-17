# Acceptance criteria for the potluck drift-11a fix (rewritten against post-5805f48 API)

Fired from potluck's own repo. `./.claude/verify.sh` (potluck's) exits 0 with every suite green.

## New protocol check enforced

- `Orchestrator._run` raises `DoerProtocolViolation(missing_ids, round)` when the doer's `DoerResponse.responses` fails to cover every issue id in the reviewer's `ReviewVerdict.issues`. `run_feature` catches and converts to `Outcome.ESCALATED_NO_SIGNAL` with an `escalation_reason` that names the missing ids and the round.
- Check fires on both round-1 `respond_to_review` and any subsequent round's `respond_to_review`. If a future refactor moves the call, the invariant follows it.
- Check does NOT fire on `reviewer_followup` — the followup contract permits "concede everything."

## New logging

- `doer_protocol_violation` event (`event_version=1`) appears in `FeatureResult.debate_log` BEFORE the escalation event, with fields: `event_version`, `ts` (ISO-8601 UTC), `round`, `missing_ids` (sorted list), `verdict_issue_count`, `responded_id_count`. Follows the v2 pack/truncation conventions from `b004a233` / `a8d34d65`.

## New tests in `harness/test_orchestrator.py`

At minimum:

- **`invariant_empty_doer_response_escalates`** — doer returns `responses=[]` on a verdict with ≥ 1 issue → `outcome == ESCALATED_NO_SIGNAL` and `escalation_reason` mentions the doer protocol violation and the missing id.

Recommended (any subset the doer judges best; the acceptance is met with at least the required test plus at least one of these):

- **`invariant_partial_doer_response_escalates`** — doer responds to a strict subset of the verdict ids → same outcome, escalation_reason names the missing subset.
- **`invariant_conformant_doer_response_still_passes`** — regression guard: doer responds to every issue → normal flow, no violation raised.

## Preservation

- All existing `test_orchestrator.py` cases still pass unchanged. The current stubs (`StubDoer`) satisfy the invariant by construction.
- All `test_resolve.py` cases and the `test_runner.py` case still pass.
- No new pip dependencies. Stdlib only.
- No changes to `DoerResponse` schema, `Outcome` enum, `FeatureResult` shape, or the `MAX_ROUNDS`/`debate.max_rounds` semantics.
- No changes to `harness/runner.py`, `harness/resolve.py`, `harness/config.py`.
- The new `DoerProtocolViolation` exception may live in `harness/orchestrator.py` (alongside the loop it applies to) or in `harness/runner.py` next to `TimeoutEscalation`/`ModelUnavailable`. Either is acceptable; pick one and document briefly.

## Runtime

- `./.claude/verify.sh` (potluck's own gate) exits 0.
- `python -m harness.test_orchestrator` prints `ALL PASS` with a case count ≥ existing + at least 1 (the required new invariant).
- `python -m harness.test_resolve` and `python -m harness.test_runner` unchanged.
