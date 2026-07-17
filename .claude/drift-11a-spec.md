# Fix (upstream, for potluck): empty DoerResponse to a non-empty verdict must escalate, not PASS

> Rewritten against post-5805f48 API surface. Original spec (pre-rewrite) was
> written against `respond_to_review(spec, verdict)`; upstream 5805f48 threaded
> `acceptance` and `diff` through every reviewer/doer/tiebreaker method, so
> `respond_to_review` now takes `(spec, acceptance, diff, verdict)`. Invariant
> is unchanged; implementation site's surface moved.
>
> Fire from **potluck's own repo** (`/Users/gpatley/workspace/masala/potluck`).
> Copy this file and the acceptance file next to it into `potluck/.claude/`
> before firing, or pass absolute paths from menu.

## The bug — the one remaining protocol hole that can produce a false PASSED

potluck's `Orchestrator._run` loop treats an empty `DoerResponse.responses` list as a valid outcome from `respond_to_review`. Concretely: when the reviewer produces a `ReviewVerdict` with N ≥ 1 blocking-or-major issues (`has_blocking_or_major` fires, debate begins), and the doer's `DoerResponse` comes back with `responses=[]`, the loop computes `response.rejected_ids() = {}`, `rejected = {} & unresolved_blocking = {}`, then `if not rejected: unresolved_blocking = set(); break`. The debate ends silently. `apply_fixes(sorted(accepted_issues, ...))` is called with the empty set — a no-op. The post-fix gate passes trivially (nothing changed). The loop returns `PASSED`.

This is a first cousin of "timeout = approved," and it violates the spirit of guarantee 2 (`schemas.py`): the verdicts are structured *precisely so the orchestrator can check them*, and here the orchestrator isn't checking. A doer that returns an empty `DoerResponse` in response to a non-empty verdict is emitting no signal on the reviewer's issues — the correct treatment is the same as any other no-signal path: escalate, never approve.

Observed in production while using potluck to build `menu`: the loop returned PASSED on a change that an independent Codex review then flagged with two BLOCKING false-negative bugs plus a missed acceptance criterion. The doer had returned `{"responses": []}`; the loop had no defence.

Drift-12 (top-level array tolerance in `_parse_verdict`) closed the OTHER protocol-shape hole that could cause loss of signal. Drift-11a closes the LAST hole where the loop can end in `PASSED` on an untouched-by-doer non-empty verdict.

## The fix — enforced at the doer-response boundary

The invariant to enforce: **every issue id in `verdict.issues` must appear as an `id` in `response.responses`**. A `DoerResponse` that omits any of the reviewer's issue ids is malformed protocol — the doer is required to take a stance (accept / reject) on each issue the reviewer raised.

### 1. Enforce at protocol level in `harness/orchestrator.py`

Inside `Orchestrator._run`, immediately after the `respond_to_review` call and BEFORE the existing debate-log / accepted_issues accounting, add a check:

- Compute `expected_ids = {i.id for i in verdict.issues}` and `responded_ids = {r.id for r in response.responses}`.
- If `expected_ids - responded_ids != set()`, treat as protocol violation. **Do NOT** silently swallow — raise a new exception `DoerProtocolViolation(missing_ids: set[str], round: int)` that the outer `run_feature` catches and converts to `Outcome.ESCALATED_NO_SIGNAL` with a descriptive `escalation_reason` naming the missing ids and the round.
- Rationale: this is neither `TimeoutEscalation` (nothing hung) nor `ModelUnavailable` (the model responded, just non-conformantly). The semantic bucket is still no-signal — the model produced structured output that failed the protocol contract — and `ESCALATED_NO_SIGNAL` already fits. Do NOT introduce a new `Outcome` value.

The check is one-directional (`expected - responded`). A doer that responds to an id NOT in the verdict (hallucinated id) is a different bug and is out of scope for this fix.

### 2. Log the protocol violation

Emit a structured `doer_protocol_violation` event to `debate_log` BEFORE the exception is raised, so the log captures the failure even if the exception path itself logs nothing further. Follow the v2 log conventions from `b004a233` / `a8d34d65` (ISO-8601 `ts`, `event_version=1`, per-string 16 KB truncation via the existing helpers if inherited):

```
{
  "event": "doer_protocol_violation",
  "event_version": 1,
  "ts": "<iso8601>",
  "round": <int>,
  "missing_ids": [<sorted list of ids>],
  "verdict_issue_count": <int>,
  "responded_id_count": <int>
}
```

### 3. Do NOT extend the check to `reviewer_followup`

The followup reviewer's contract is different — the followup prompt (`_review_followup_prompt`) tells the reviewer "return ONLY the same JSON verdict schema, containing only the issues you still hold." A followup that returns zero held issues is a legitimate concession-of-everything, not a protocol violation. Only the DOER's obligation to respond to every verdict issue is enforced. If a followup-reviewer analogue is needed later, that's a separate fix.

### 4. Preserve backward-compat for all existing tests

None of the current `test_orchestrator.py` cases should regress. Read `StubDoer.respond_to_review` first: the stub iterates `verdict.issues` and produces an `IssueResponse` for each, so it satisfies the invariant by construction. Verify with a run before adding new tests.

## What to add in tests (`harness/test_orchestrator.py`)

Required:

- **`invariant_empty_doer_response_escalates`** — construct a stub doer (`EmptyResponseDoer` or an ad-hoc subclass) that returns `DoerResponse(responses=[])` from `respond_to_review(spec, acceptance, diff, verdict)` regardless of the verdict. Wire it into an orchestrator with a reviewer that emits ONE blocking issue on the initial review. Run `run_feature(spec, acceptance)`. Assert `result.outcome == Outcome.ESCALATED_NO_SIGNAL` and `result.escalation_reason` contains a substring identifying it as a doer protocol violation naming the missing issue id.

Recommended (the acceptance is met with at least the required case plus at least one of these):

- **`invariant_partial_doer_response_escalates`** — same setup, reviewer emits TWO blocking issues, doer responds to only one. Same expected outcome; escalation reason names the missing subset.
- **`invariant_conformant_doer_response_still_passes`** — regression guard: doer responds to every issue, loop behaves normally (essentially the existing `convergence` test — this ADDS an explicit assertion that a conformant response does NOT trigger the violation).

## What NOT to change

- Do NOT change the `DoerResponse` schema in `harness/schemas.py`. The invariant is a runtime protocol check, not a schema-level requirement — the schema still needs to accept `responses=[]` at parse time (for legitimate paths like an early-exit where the doer is never called with a non-empty verdict).
- Do NOT modify `MAX_ROUNDS` or `debate.max_rounds` semantics. Orthogonal to the round cap.
- Do NOT add new fields to `FeatureResult`. Route the failure through `escalation_reason`.
- Do NOT touch `harness/runner.py`, `harness/resolve.py`, `harness/config.py`. The change is localized to `orchestrator.py` (control flow + logging) and `test_orchestrator.py` (new tests).
- Do NOT add pip dependencies.

## Compat notes for the post-5805f48 API surface

- `respond_to_review` signature is `(spec: str, acceptance: str, diff: str, verdict: ReviewVerdict)`. Your check reads `verdict.issues` (unchanged) and `response.responses` (unchanged). No API surface change needed for the check itself.
- `apply_fixes` now takes `list[ReviewIssue]` (full objects, not IDs). Your fix does not change apply_fixes behaviour — the protocol violation escalates BEFORE the loop reaches apply_fixes.
- Follow the v2 debate-log format for the new `doer_protocol_violation` event.
