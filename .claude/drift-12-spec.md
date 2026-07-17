# Fix (upstream, for potluck): `_parse_verdict` must accept a top-level JSON array as shorthand for `{"issues": [...]}`

> Fire from **potluck's own repo** (`/Users/gpatley/workspace/masala/potluck`).
> Copy this file and the acceptance file next to it into `potluck/.claude/`
> before firing, or pass absolute paths from menu.

## The bug — and why "not urgent" is the wrong triage

potluck's `_parse_verdict` calls `_extract_json`, which looks for the first `{` and last `}` in the model's output. If the model returns a top-level JSON array (bare `[]` or `[{"id": "I1", ...}]`), `_extract_json` finds no `{...}` window and raises `ValueError: no JSON object found in model output: []`. The exception propagates up through `_review_followup` → `_run` → `run_feature`, which catches only `TimeoutEscalation` and `ModelUnavailable` — so the whole loop crashes with an unhandled traceback and exits non-zero. Observed on the menu traceability-slice fix loop, where Codex returned literally `[]` as its followup verdict.

The subtle part: **bare `[]` is the happy-path shape of every clean followup review.** "No remaining issues held" is what a followup reviewer is prompted to return when the doer's rebuttal was accepted (per `_review_followup_prompt`, which asks the reviewer to "return ONLY the same JSON verdict schema, containing only the issues you still hold"). So the loop crashes preferentially when the reviewer is SATISFIED, not when the reviewer finds problems. This is a broken main line, not an edge case, and it will hit every future debate round where the reviewer converges.

## The fix — narrow and precise

The tolerance change lives in **`_parse_verdict` specifically**, not in `_extract_json` generally. `_extract_json` is used by `_parse_tiebreak` too, and a tiebreak returning a top-level array does NOT make semantic sense (tiebreaks are per-issue and return an object with `sides_with`). Broadening `_extract_json` risks silently accepting malformed tiebreak output.

In `harness/orchestrator.py`:

- Add a helper `_extract_verdict_json(text: str) -> dict` (or inline in `_parse_verdict`) that:
  1. Strips fences and prose the same way `_extract_json` does — reuse its whitespace / triple-backtick handling for the leading portion.
  2. Detects whether the stripped payload starts with `[` (top-level array) or `{` (object).
  3. If array: parse as a list and coerce to `{"issues": <list>}` before validation.
  4. If object: fall through to the existing `_extract_json` path.
- `_parse_verdict` calls this helper instead of `_extract_json` directly.
- The array-form tolerance applies to BOTH the initial `_review` verdict and the `_review_followup` verdict (both go through `_parse_verdict`).

The `_extract_json` function itself is left untouched — `_parse_tiebreak` continues to use it and continues to demand `{...}`. A future finding about tiebreak tolerance would be its own fix.

Do **not** widen tolerance to accept:
- A payload that has non-JSON prose ONLY, with no bracket of either kind. That's a genuine no-signal case and should still raise.
- Nested/wrapped shapes like `{"data": [...]}` or `[{"issues": [...]}]`. Only bare `[]` and `[{...}]` at the top level are the observed model behaviour; broader tolerance invites false-positive parses.

## What to add in tests (`harness/test_orchestrator.py`)

Two regression cases minimum:

- **`invariant_bare_empty_array_verdict_parses`** — `_parse_verdict("[]")` returns a `ReviewVerdict` with `issues=[]`. Assert no exception; assert `verdict.issues == []`.

- **`invariant_bare_populated_array_verdict_parses`** — `_parse_verdict('[{"id": "I1", "severity": "blocking", "issue": "x", "suggested_fix": "y"}]')` returns a `ReviewVerdict` with one issue, id `"I1"`, severity `"blocking"`. Assert full validation, not just parse.

Plus one integration-level guard, if easy to express with existing stubs:

- **`invariant_followup_bare_array_ends_debate_cleanly`** — set up an orchestrator whose followup reviewer stub returns literal string `"[]"` (a `StubReviewer` variant with `followups=["[]"]`, feeding through `respond` → `_parse_verdict` → the loop). The loop should NOT crash; it should observe `verdict.issues == []` and exit the debate cleanly with `PASSED`. This is the end-to-end test that the failure mode observed on menu is now closed.

## What NOT to change

- Do NOT modify `_extract_json` — the change is verdict-specific per the reasoning above.
- Do NOT modify `_parse_tiebreak` — tiebreaks require the object shape by their semantics.
- Do NOT modify `ReviewVerdict` or any other schema in `harness/schemas.py`. The tolerance is at the parse boundary, not the schema.
- Do NOT add new `Outcome` values; the fix is orthogonal to outcome semantics.
- Do NOT add pip dependencies. Stdlib `json` only.
