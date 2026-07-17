# Acceptance criteria for the potluck drift-12 fix

Fired from potluck's own repo. `./.claude/verify.sh` (potluck's) exits 0 with every suite green.

## New tolerance in `_parse_verdict`

- `_parse_verdict("[]")` returns a `ReviewVerdict` with `issues=[]`.
- `_parse_verdict('[{"id": "I1", "severity": "blocking", "issue": "x", "suggested_fix": "y"}]')` returns a `ReviewVerdict` with one issue, id `"I1"`, severity `"blocking"`, and the other fields intact.
- `_parse_verdict` continues to accept the existing object form `{"issues": [...]}` unchanged.
- Tolerance is scoped to `_parse_verdict` only; `_parse_tiebreak` behaviour is untouched. `_extract_json` remains as it is (no top-level array tolerance) so `_parse_tiebreak` continues to reject malformed tiebreak output.

## New tests in `harness/test_orchestrator.py`

At minimum:

- **`invariant_bare_empty_array_verdict_parses`** — direct-call assertion on `_parse_verdict("[]")`. Case count grows by at least 1.
- **`invariant_bare_populated_array_verdict_parses`** — direct-call assertion on `_parse_verdict('[...]')` with one full issue object. Full schema validation must succeed.
- **`invariant_followup_bare_array_ends_debate_cleanly`** — integration test: an orchestrator whose followup reviewer stub returns literal `"[]"` completes with `Outcome.PASSED` and does NOT raise. This is the end-to-end regression against the menu traceability-slice crash.

## Preservation

- All 13 existing `test_orchestrator.py` cases still pass unchanged.
- All 16 `test_resolve.py` cases still pass. All `test_runner.py` cases still pass.
- No changes to `harness/runner.py`, `harness/resolve.py`, `harness/config.py`, `harness/schemas.py`, or `_parse_tiebreak` in `harness/orchestrator.py`.
- No new pip dependencies (stdlib `json` only, already imported).

## Runtime

- `./.claude/verify.sh` (potluck's own gate) exits 0.
- `python -m harness.test_orchestrator` prints `ALL PASS` with a case count ≥ 16 (13 existing + at least 3 new; if the doer adds only the two required parse-level cases plus one integration case, that's 16).
- `python -m harness.test_resolve` and `python -m harness.test_runner` unchanged.
