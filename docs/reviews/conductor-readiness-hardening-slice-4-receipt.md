# conductor-readiness hardening slice §4 (potluck) — code-review receipt

The machine-checkable block below is what the reviewgate pre-push hook validates.
It authorizes the potluck half of §4 (decider item 4, §4b): the full
`git diff 665a56a..b2ed986` — the `doer:implement` prompt now steers toward the
smallest sufficient diff that satisfies the spec AND the acceptance criteria (no
speculative abstraction, no unrequested surface). A prompt discipline, not a
machine-enforced size guarantee.

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: b2ed9863acce6fa1ee61123081d76cb59f49fe6d
verdict: SHIP
date: 2026-07-25
findings: docs/reviews/conductor-readiness-hardening-slice-4-code-review.md
```

**Verdict wording.** codex's verbatim round-1 verdict was **`RATIFIED`** (0 findings),
reproduced in the findings pointer. reviewgate's only authorization token is `SHIP`
(`_is_ship`), so the machine block records `verdict: SHIP` as the authorization
*class*; RATIFIED is the reviewer's literal clean-verdict word (the RATIFIED→SHIP
mapping is the standing decider ruling of 2026-07-24).

**Provider independence (G4/G5).** Reviewer codex (OpenAI, `gpt-5.6-sol`) ≠ the doer
(Claude/Anthropic). Verdict is SHIP-class.

**Range identity (obs-021 / obs-025).** `reviewed_head` names this commit's parent
(`b2ed986`). The reviewed range is `665a56a..b2ed986`, and origin/main sat at
`665a56a` (the §5 receipt tip) at landing, so the pushed range equals the reviewed
range. §4 lands under its OWN receipt, separate from prior slices. `harness_core`
untouched (FENCE, Step-0 §9).

**Coupled unit (one review, two repos).** This is the potluck (§4b) half of a single
codex review that read BOTH repos' §4 ranges; the menu (§4a) half lands under its own
same-content receipt in the menu repo — a receipt covers exactly what its review read
(obs-025), and this one review read both.

**Prompt discipline, no enforcement (Ruling B).** §4 adds steering language only; the
slice claims NO quantitative enforcement (one real doer call can't prove minimal-diff,
round-1 MINOR-7). §5's live-doer smoke is the qualitative guard on this behaviour.

## Token spend (disclosed)

codex `exec` (gpt-5.6-sol), §4 CODE review, 1 round (one review over both repos):
**43,642 tokens**. All build work was $0 internal-dev (`authorized_act =
{deploys:false, publishes:false, spends:false}` → `internal_dev`).
