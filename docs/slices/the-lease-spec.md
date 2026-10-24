# Slice: the lease — one attempt per admission, a 1200s implement

**Authority: `conductor/docs/decisions/002-early-signal.md`, ruling R3 and R5** (in the sibling
`conductor` checkout; read it, do not edit it). Where this spec and the ruling disagree, the ruling
wins and the disagreement is a finding. The evidence and reasoning live there; this file is
instruction only.

## What this slice builds

1. **potluck makes exactly one attempt per bounded call.** potluck's own config resolves
   `escalation.retries_before_counting = 0`: change the `escalation` field of `HarnessConfig` in
   `harness/config.py` to default to an `EscalationConfig` with `retries_before_counting=0`, and set
   `retries_before_counting = 0` in all three shipped profiles (`profiles/{personal,phi,ci}/profile.toml`).
   **Do not change `harness_core`** — its default stays 1, because menu consumes it.

2. **`retries_before_counting` joins the run's committed identity.** Add it to the dict returned by
   `_effective_config_dict` in `harness/cli.py`, so a resume across a change of retry policy is
   refused by the existing identity check exactly as a changed timeout is.

3. **`doer:implement` rises from 600 to 1200 seconds** in `doer_step_seconds`. `doer:apply_fixes` and
   `doer:respond` stay 600.

4. **`feature_seconds` is restated to satisfy the existing outer bound** — nothing recomputes it:
   `c_max = 1200 × 2 + 5 = 2405`. The dataclass default and the `personal` and `phi` profiles become
   **72150** (2405 × N_max 30). The `ci` profile becomes **33670** (2405 × N_max 14; its gate stays
   600). Update the explanatory comments beside each value so none still says 1205, 36150 or 16870.

5. **The implement prompt is measured.** `doer:implement`'s assembled prompt emits the same
   `prompt_size` event (and `prompt_large` above `large_prompt_warn_bytes`) that `_assemble_prompt`
   emits for other steps, with `step="doer:implement"`. Measure the exact bytes sent, without
   changing the prompt's text. **Record only: nothing refuses or warns on its value beyond the
   existing `prompt_large` event.**

6. **Superseded pins are superseded in writing.** `docs/slices/respond-timeout-band-spec.md` and
   `respond-timeout-band-acceptance.md` state the old implement and feature values as landed facts.
   Do not rewrite them; append to each a short dated note that ruling 002 (this slice) supersedes
   those values, naming the new ones.

## Tests

Update the tests that pin the old literals to the new truth — `test_doer_timeout.py`'s default-map
assertion for implement, `test_call_budget.py`'s default `c_max` and the `R=2` arithmetic — and keep
the 600 assertions for `apply_fixes` and `respond`. Fixtures that set 600 explicitly to test override
behaviour stay as they are. Add tests for: every shipped profile loading and validating; one bounded
call making exactly one attempt on timeout under potluck's config (and two under a bare
`harness_core` `EscalationConfig`, proving the core default is untouched); a stubbed implement that
takes longer than 600s of simulated time completing without being restored; identity refusing a
resume whose only difference is `retries_before_counting`; and the implement `prompt_size` event.

**For every new test, state in a comment what would have to break for it to fail.** A test that
cannot fail is a finding.

## What must not change

* `harness_core`, `menu`, `conductor` — untouched. One repository: potluck only.
* `station-run.json` stays v1. No new event types; `prompt_size` already exists.
* `validate()`'s rules are unchanged; only the values that must satisfy them move.

## Review budget

**2 rounds, declared.** An overrun is declared in the receipt.
