# conductor-readiness hardening slice §5 (potluck) — code-review receipt

The machine-checkable block below is what the reviewgate pre-push hook validates.
It authorizes the potluck half of §5 (decider item 5): the full
`git diff 8a2ea13..6a64341` — a new env-gated `test_live_doer.py` (one real
`doer:implement` call through the real budgeted-runner + `RealDoerClient` path,
asserting timeout-adherence + well-formed output, logging size/diff-scope
observationally) plus the obs-017 declared-gap comment update.

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 6a64341a212273bb3e11076758f45898b4eb9911
verdict: SHIP
date: 2026-07-25
findings: docs/reviews/conductor-readiness-hardening-slice-5-code-review.md
```

**Verdict wording.** codex's verbatim round-2 verdict was **`RATIFIED`** (0 findings),
reproduced in the findings pointer. reviewgate's only authorization token is `SHIP`
(`_is_ship`), so the machine block records `verdict: SHIP` as the authorization
*class*; RATIFIED is the reviewer's literal clean-verdict word (the RATIFIED→SHIP
mapping is the standing decider ruling of 2026-07-24).

**Provider independence (G4/G5).** Reviewer codex (OpenAI, `gpt-5.6-sol`) ≠ the doer
(Claude/Anthropic). Verdict is SHIP-class.

**Range identity (obs-021 / obs-025).** `reviewed_head` names this commit's parent
(`6a64341`). The reviewed range is `8a2ea13..6a64341`, and origin/main sat at
`8a2ea13` (the §1+§2 receipt tip) at landing, so the pushed range equals the reviewed
range. §5 is a SEPARATE concern from §1+§2, landed under its OWN receipt — a receipt
covers exactly what its review read. `harness_core` untouched (FENCE, Step-0 §9).

**Live smoke evidence (why §5 exists).** Both suites are offline/mock, which is why
the first customer hit walls the mocks could not catch. §5's env-gated smoke, run live
this session: potluck `doer:implement` completed in 7.2s of its 600s budget (1% of the
C_max ceiling), a 77-char well-formed reply, 1 real path edited in an isolated temp
repo. This grounds the C_max per-call ceiling in a real provider call.

**obs-017, partially retired (honest scope, obs-025).** §5 retires the "no live doer
evidence" part of the declared N_max gap — C_max is now anchored to a real call. What
STAYS structural: the `≤ N_max` COUNT is still a formula + a stub-run assertion (T4b),
NOT a live full-debate call count (one smoke call cannot exercise the multi-round
path). The residual is safe in the same restrictive direction (over-refuse →
resumable `ESCALATED_CALL_BUDGET`, never a silent overrun).

## Token spend (disclosed)

codex `exec` (gpt-5.6-sol), §5 CODE review, 2 rounds (one review over both repos): r1
47,162 + r2 29,382 = **76,544 tokens**. The two live smoke calls (Claude, doer) were
internal-dev doer calls, not review spend. All build/fold work was $0 internal-dev
(`authorized_act = {deploys:false, publishes:false, spends:false}` → `internal_dev`).
