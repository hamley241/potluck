# /potluck-bugfix — convergent bug-fix loop

Fix a bug under the harness's control guarantees. The defining gate for a bug
fix is a FAILING TEST THAT REPRODUCES THE BUG turning green, plus the full suite
staying green.

Work in this order:

1. REPRODUCE FIRST. Write a test that fails because of the bug. Do not touch the
   fix until you have a red test that captures the actual defect. If you cannot
   reproduce it, STOP and report what you tried — do not guess at a fix.

2. Fix the root cause, not the symptom. The reviewer will specifically check
   whether the fix addresses the underlying cause or just silences the
   reported symptom, and whether it risks regressions elsewhere.

3. Drive the gate to green: ./.claude/verify.sh must exit 0 (the reproducing
   test now passes AND the full suite/typecheck/lint/build still pass).

4. Obey the rules in rules/ without exception:
   - never edit a test or config to make the gate pass (no-gate-tampering)
   - never commit/push with --no-verify (no-skip-verify)

5. When green, the harness sends your diff to an independent reviewer. Respond
   to each issue honestly: accept (with a fix plan) or reject (with reasoning).
   You are expected to push back when the reviewer is wrong in context — do not
   "fix" things that aren't broken just to agree.

STOP CONDITIONS (the harness enforces these; respect them):
- same gate failure twice in a row → stop, report (likely environment)
- unresolved blocking disagreement after the round cap → surfaced to human
- repeated timeouts → surfaced to human
Leave work committed so it can be resumed. Do not declare done until the gate
is green AND the debate is resolved.

BUG REPORT:
$ARGUMENTS
