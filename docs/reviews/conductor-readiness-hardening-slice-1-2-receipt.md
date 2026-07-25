# conductor-readiness hardening slice §1+§2 (potluck) — code-review receipt

The machine-checkable block below is what the reviewgate pre-push hook validates.
It authorizes the potluck half of the coupled §1+§2 hardening slice: the full
`git diff c2a1d01..ae91ac2` — step-adaptive doer timeouts + `--doer-timeout` (§1.1/
§1.2), the outer-bound pre-dispatch call-budget `outer_min = C_max × N_max` (§1.3),
the typed `ExecCursor` + atomic fail-closed checkpoints (§2.0/§2.1), lossless
git-tree resume (§2.3), and resume backoff (§2.4).

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: ae91ac25d7c30cb1de3ad857cb962fd586d61c0f
verdict: SHIP
date: 2026-07-24
findings: docs/reviews/conductor-readiness-hardening-slice-1-2-code-review.md
```

**Verdict wording (transparent mapping).** codex's verbatim round-12 code-review
verdict was **`RATIFIED`** — `0 BLOCKING / 0 MAJOR / 0 MINOR / 0 NIT`, reproduced
verbatim in the findings pointer above. reviewgate's only authorization token is
`SHIP` (`_is_ship`), so the machine block records `verdict: SHIP` as the
authorization *class*; RATIFIED is the reviewer's literal clean-verdict word. The
RATIFIED→SHIP mapping was a decider ruling (2026-07-24), not a builder judgment.

**Provider independence (G4/G5).** Reviewer codex (OpenAI, `gpt-5.6-sol`) is a
different provider than the doer (Claude/Anthropic). The verdict is SHIP-class (G5).

**Range identity (obs-021 / obs-025).** `reviewed_head` names this commit's parent
(`ae91ac2`, the parent form, §A.7c) — the tip of the reviewed work. The reviewed
range is `c2a1d01..ae91ac2`, and origin/main sat at `c2a1d01` at landing, so the
pushed range equals the reviewed range: every commit in the push is within the
review's actual reach, and both potluck reviewed surfaces (the engine-adjacent
`harness/` and the CLI, obs-018) are in the reviewed bytes because the packet is
the whole-range `git diff`, not a hand-assembled subset. `harness_core` is
untouched (the FENCE held, per Step-0 §9).

**Coupled unit (one review, two repos).** This is the potluck half of a single
codex review that read BOTH repos' ranges each round; the menu half lands under its
own same-content receipt in the menu repo (a receipt covers exactly what its review
read, obs-025 — and this one review read both).

**Declared gap carried forward (obs-017 / §5).** The `N_max` pins are
**stub-measured**, pending §5's live-doer justification. codex was told each round
to treat this as note-only (accepted, decider ruling (b)); it was not re-raised as
blocking at round 12. Blast radius, worst case: a premature but fully resumable
`ESCALATED_CALL_BUDGET`. §5 (the live-doer smoke over both stations, which pins
`N_max` from a real run) is the IMMEDIATE next work item after this landing — no
queue-jumping into §3/§4/§6.

## Token spend (disclosed)

codex `exec` (gpt-5.6-sol), §1+§2 CODE review, 12 rounds: r1 137,383 + r2 73,670 +
r3 100,214 + r4 100,363 + r5 86,824 + r6 89,899 + r7 80,027 + r8 80,262 + r9 89,659
+ r10 110,148 + r11 82,718 + r12 96,692 = **1,127,859 tokens**. All build and fold
work was $0 internal-dev (`authorized_act = {deploys:false, publishes:false,
spends:false}` → `internal_dev`); nothing deployed/published/spent. The entire
external spend is the review/ratification, not the build — the same usage-pattern
datum the Step-0 ratification first surfaced, now at code scale: a coupled
two-station hardening slice cost ~1.13M review tokens to ratify and zero to build.
