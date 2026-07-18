# Fix (upstream, for potluck): distinguish "gate could not run" from "gate reported failure"

> Fire from **potluck's own repo** (`/Users/gpatley/workspace/masala/potluck`).
> Copy this file and the acceptance file next to it into `potluck/.claude/`
> before firing, or pass absolute paths from menu.

## The bug

`_gate_or_escalate` in `harness/orchestrator.py` collapses two structurally different signals into one outcome. A gate that RAN and reported failure (`verify.sh` exit 1: tests actually failed) and a gate that COULD NOT RUN (`verify.sh` exit 127: missing interpreter/tool/venv/uv) both land as `ESCALATED_GATE` with reason `"gate could not be made green before review"`.

Observed twice on menu's drift-11a fix loop and once on drift-12: the doer's code was correct, all downstream tests passed when invoked directly, but potluck's own `./test` gate needs `uv` (not installed on the observation machine). The gate stderr was `uv: command not found` — an environmental problem, not a code problem — but the loop reported `ESCALATED_GATE`, which points every debugging effort at the doer's supposedly-broken code.

These are different signals deserving different outcomes: a gate that can't execute is **no signal on correctness**, not red. potluck already honors exactly this distinction for gate *timeouts* — a hung gate raises `TimeoutEscalation("gate_no_signal", …)` rather than being misread as failure (`harness/orchestrator.py:283–303` in the pre-drift-11a version; renumber against current HEAD before touching). The env-missing case is the same species and deserves the same treatment.

## The fix — small, precise, matches the existing timeout precedent

### Convention

`verify.sh` (and any gate in this family) uses a distinguished exit code — proposal: **`2`** — for "environment unusable / gate could not run." The convention is documented in `verify.sh.example` and in `README.md`'s gate section. The interpretation is: exit 0 = green (correctness settled), exit 1 = red (checks reported failure), exit 2 = no signal (checks could not execute).

### Orchestrator mapping

`_gate_or_escalate` inspects the gate's `GateResult.ok` and, when not ok, the `GateResult.output` (which now carries `exit_code` for this distinction — see the schema note below).

- Exit 0 → gate passed as today.
- Exit 1 → `ESCALATED_GATE` as today (real correctness failure).
- **Exit 2** → treat as no-signal: escalate as `Outcome.ESCALATED_NO_SIGNAL` (reuses the existing bucket) with an `escalation_reason` naming the gate label AND surfacing the gate's stderr/stdout so the reader sees the missing tool. Do NOT introduce a new `Outcome` value; the semantic bucket already exists (timeouts and unavailable models both route through it).
- Any other non-zero exit code → conservatively fall back to `ESCALATED_GATE` (unknown exit codes are treated as red — the doer's contract is to make the gate green; garbage exit codes are the doer's problem to investigate).

### `GateResult` schema change

`GateResult` currently is `(ok: bool, output: str)`. Add an `exit_code: int` field so the orchestrator can distinguish exit 1 from exit 2. Default value should be `1` when `ok=False` so existing tests that construct `GateResult(ok=False, output="...")` continue to behave as "red." Existing `GateResult(ok=True, output="...")` behaviour is unchanged.

### `real_run_gate` change

`real_run_gate` in `harness/orchestrator.py` currently returns `GateResult(ok=proc.returncode == 0, ...)`. Change to also populate `exit_code=proc.returncode` so the orchestrator sees the actual code.

### `verify.sh.example` change

Extend the template to demonstrate the exit-2 convention:

```bash
# Fail closed on environment problems — exit 2 signals "gate could not run",
# which the orchestrator maps to ESCALATED_NO_SIGNAL (not red). This keeps
# real correctness failures (exit 1) distinguishable from missing-tool /
# missing-venv / missing-interpreter failures.
if ! command -v uv >/dev/null 2>&1; then
  echo "verify.sh: 'uv' not found on PATH — cannot run test suites" >&2
  exit 2
fi
```

And a matching README note documenting the convention.

## What NOT to change

- Do NOT change `TimeoutEscalation` or the timeout paths — the timeout escalation is the reference implementation this fix is modelling after; leave it alone.
- Do NOT introduce a new `Outcome` value. `ESCALATED_NO_SIGNAL` already exists and its semantic ("we did not get the signal we needed from this step") fits.
- Do NOT modify `_parse_verdict`, `_parse_tiebreak`, the debate loop, or the doer/reviewer/tiebreaker protocols. Orthogonal.
- Do NOT change how the gate `output` field is packed into logs. `GateResult.to_json_str()` should serialise `exit_code` alongside `ok` and `output`; consumers that need the distinction can read it, existing consumers ignoring it stay compatible.

## What to add in tests (`harness/test_orchestrator.py`)

Two new invariants minimum:

- **`invariant_env_unusable_gate_no_signal`** — a gate stub returning `GateResult(ok=False, output="verify.sh: uv not found", exit_code=2)` on initial run → `outcome == ESCALATED_NO_SIGNAL` and `escalation_reason` mentions the gate label AND surfaces the stderr text (proves the log carries the environmental context).

- **`invariant_gate_red_still_escalates_gate`** — regression guard: a gate stub returning `GateResult(ok=False, output="1 test failed", exit_code=1)` → `outcome == ESCALATED_GATE` (existing behaviour preserved).

Recommended:

- **`invariant_unknown_gate_exit_code_treated_as_red`** — `exit_code=7` → `ESCALATED_GATE` (conservative fallback for garbage exit codes).

## Compat with prior invariants

- `invariant_silent_green_gate` (`GateResult(ok=True, output="")`) — still passes as `Outcome.PASSED`; exit code defaults to 0.
- `invariant_gate_failure` — still passes as `Outcome.ESCALATED_GATE`; adapter should build `GateResult(ok=False, output="", exit_code=1)`.
- All existing gate-fixture wiring (`gate_pass`, `gate_fail`, `gate_hang` in `harness/test_orchestrator.py`) should be reviewed — either updated to populate `exit_code` explicitly, OR rely on the default (`0` when `ok=True`, `1` when `ok=False`) which preserves current test expectations.
