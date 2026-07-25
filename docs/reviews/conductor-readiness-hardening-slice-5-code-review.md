# conductor-readiness hardening slice §5 — codex code-review findings (verbatim)

Independent, cross-provider CODE review of **§5 — one env-gated live-doer smoke test
per station** (decider item 5, resolves round-1 MINOR-7), by **codex (OpenAI), model
`gpt-5.6-sol`** — read-only sandbox (`codex exec -s read-only`), doer Claude
(Anthropic). One review over BOTH repos' §5 ranges (potluck `8a2ea13..6a64341`, menu
`767e4b4..d7ee449`). Two rounds; verdicts reproduced VERBATIM below. `harness_core`
untouched (the FENCE held). §5 is a SEPARATE concern from §1+§2, landed under its own
receipt/OID range (obs-025).

Live smoke evidence produced this session (the point of §5 — mocks miss length/latency
walls): menu `doer:sketch` 6.3s of 600s (1%), 284-char body under the 400 cap;
potluck `doer:implement` 7.2s of 600s (1%), 77-char reply, 1 real path edited. Both
real doer calls completed well within the adaptive timeout with well-formed output.

---

## Round 1 — verbatim

> BLOCKING
>
> - None.
>
> MAJOR
>
> - [potluck/harness/test_live_doer.py:73](/Users/gp/workspace/masala/potluck/harness/test_live_doer.py:73), [menu/harness/test_live_doer.py:49](/Users/gp/workspace/masala/menu/harness/test_live_doer.py:49) — the opt-in gate treats every non-empty value as enabled, rather than requiring the specified `POTLUCK_LIVE=1` / `MENU_LIVE=1`. Concrete failure: a CI environment sets `MENU_LIVE=0` to document that live tests are disabled; the test nevertheless makes a billable 600-second-cap provider call. Compare explicitly with `"1"`.
>
> MINOR
>
> - None.
>
> NIT
>
> - None.
>
> VERDICT: `CHANGES REQUIRED`

---

## Round 2 — verbatim

> BLOCKING
>
> None.
>
> MAJOR
>
> None. The round-1 gate defect is fixed in both files: unset or `"0"` skips; only the exact value `"1"` permits a live provider call.
>
> MINOR
>
> None.
>
> NIT
>
> None.
>
> The ranges touch only `harness/config.py` and `harness/test_live_doer.py`; fenced `harness_core` remains untouched. Both station tests use the real budgeted runner/client path, resolve the adaptive timeout, keep qualitative size/diff evidence observational, and isolate potluck’s editing call in a temporary repository.
>
> VERDICT: `RATIFIED`

---

**Ratified at round 2, codex CLEAN (0 findings), VERDICT: RATIFIED.** Recorded as the reviewgate SHIP-class landing authorization (SHIP is the gate's only token; RATIFIED is codex's verbatim clean word).
