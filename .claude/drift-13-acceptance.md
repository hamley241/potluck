# Acceptance criteria for the potluck drift-13 fix

Fired from potluck's own repo. `./.claude/verify.sh` (potluck's) exits 0 with every suite green.

## New exit-code convention documented

- `verify.sh.example` demonstrates the exit-2 convention (or equivalent — the point is to make the convention visible in the reference template).
- `README.md` (or `WIRING.md`, whichever is more idiomatic) has one paragraph documenting the exit-code convention: 0 = green, 1 = red, 2 = no signal (environment unusable), other = conservatively treated as red.

## Schema and code changes

- `GateResult` gains an `exit_code: int` field. Default value `1` when `ok=False` (preserves existing behaviour of tests that construct `GateResult(ok=False, output="…")` without an exit code); default `0` when `ok=True`.
- `real_run_gate` populates `exit_code=proc.returncode`.
- `_gate_or_escalate` maps `exit_code == 2` on a non-`ok` result to `ESCALATED_NO_SIGNAL` (with `escalation_reason` including the gate label and the gate stderr/output), and preserves the current `ESCALATED_GATE` for `exit_code == 1` or any other non-zero code.

## New tests in `harness/test_orchestrator.py`

Required:

- **`invariant_env_unusable_gate_no_signal`** — gate stub returns `GateResult(ok=False, output="<env problem text>", exit_code=2)` on the initial gate run → `outcome == Outcome.ESCALATED_NO_SIGNAL`, and `escalation_reason` mentions the gate label AND contains the env-problem text.

- **`invariant_gate_red_still_escalates_gate`** — regression guard: `GateResult(ok=False, output="1 test failed", exit_code=1)` → `outcome == Outcome.ESCALATED_GATE`.

Recommended (any of these is fine; acceptance is met with the two required cases):

- **`invariant_unknown_gate_exit_code_treated_as_red`** — `exit_code=7` conservatively → `ESCALATED_GATE`.

## Preservation

- All existing `test_orchestrator.py` cases still pass unchanged. Any existing case constructing `GateResult(ok=False, output="…")` continues to be treated as `ESCALATED_GATE` — this is why the default `exit_code=1` on `ok=False` is load-bearing.
- All `test_resolve.py` cases and the `test_runner.py` case still pass.
- No new pip dependencies.
- No changes to `harness/runner.py`, `harness/resolve.py`, `harness/config.py`, `harness/schemas.py` (unless `GateResult` lives there — if so, only the `exit_code` field is added).
- No new `Outcome` enum value.
- `_parse_verdict`, `_parse_tiebreak`, the debate loop, and the doer/reviewer/tiebreaker protocols are untouched.

## Runtime

- `./.claude/verify.sh` (potluck's own gate) exits 0.
- `python -m harness.test_orchestrator` prints `ALL PASS` with a case count ≥ existing + at least 2 (the required new invariants).
- `python -m harness.test_resolve` and `python -m harness.test_runner` unchanged.
