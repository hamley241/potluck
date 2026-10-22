# doer:respond reclassified heavy, and the band pinned — receipt (SHIP)

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 6d8d405ee907b07d1ce9dc2ec8dfde9b973a01de
verdict: SHIP
date: 2026-10-22
findings: docs/reviews/potluck-respond-timeout-band-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) ≠ doer (Anthropic). SHIP-class.
**Range identity (obs-021 / obs-025).** `reviewed_head` is the content tip; `main` at `3e6aa75`.

## What lands

`doer:respond` moves 300 → 600, joining its generative siblings. It was not an oversight: the
suite comment read *"heavy generative doer steps get 600"* and respond had 300, so it was
CLASSIFIED not-heavy. The evidence overturns the classification — respond was one of the two
steps that timed out in the first-customer report ("the single biggest friction", n=2), the
hardening slice then raised the band and left the actual casualty at its FLOOR, and a sibling
session hit the same timeout on a real converge today (n=3).

Costs nothing in the outer bound, computed not assumed: `c_max()` follows the heaviest step,
already 600, so `c_max` stays 1205 and `outer_min` 36150 against `feature_seconds` 36150.

## Panel-ruled, because it required editing a ratified test

Raising it meant changing `map_respond_300`, this project's recognised anti-pattern. codex and
kimi, asked independently, both ruled the edit correct and for the same reason: that assertion
transcribes the map's CLASSIFICATION, not an independent correctness property, and T6's
precedence contract survives the value changing. Keeping a classification the evidence
contradicts, purely to avoid touching a ratified test, is itself the locked-in bug.

## Two rounds, two MAJORs, both in the same place

Both findings were in the new check's own DEFINITION of what it governs — never in the thing it
governs — and neither could be caught by running the suite, because both passed by construction.

- **r1 `timeout-band-step-universe`** — the check read only the KEYS of `doer_step_seconds`.
  `resolve_doer_timeout` falls through to 300 for an unmapped step, so a new generative step
  never added to the map inherits 300 silently, and a key-reader cannot see it. The one case the
  test existed for was the one case it was blind to. Now also derives every `"doer:*"` literal
  used in non-test modules and requires each to be mapped.
- **r2 `timeout-band-literal-quote-gap`** — that replacement scan matched only double-quoted,
  lowercase-snake literals, so `'doer:foo'` escaped it. Both quote styles and the full
  identifier class now, with three self-checks on the pattern itself.

**Canon banked.** *A derived set is only as good as the direction you derive it in* — deriving
from the map answered "is anything in the map off-band" when the question was "is anything
off-map". And: *a stated limit can be accurate and still mislead if it is stated at the wrong
grain* — "it finds string literals" was true of a pattern that missed half of them.

## Not written by potluck, and that is itself a finding

Two `potluck fix` runs dispatched 2 doer calls and produced NO diff, terminating
`escalated_no_signal` — correctly refusing to treat an empty diff as success. The likely cause
is that my spec was **unsatisfiable**: it required respond=600 while its acceptance A8 said "no
existing test file is edited", which `map_respond_300` forbids. The doer may have been right to
refuse. potluck could not say so — it reports "produced no changes", never "your acceptance
criteria are mutually unsatisfiable", and captures no doer reply to diagnose from. Tracked in
`docs/reviews/potluck-followups.md`.

A `--resume` of that run then refused fail-closed: *"ignored files are present that
`clean -fdx` would delete"*. Also tracked — it makes resume unusable in all four fleet repos.

**Verified outside the loop.** `./test` ALL SUITES PASS. Both directions of the new check proven
able to fail against real edits to `orchestrator.py`: an off-band map entry, a double-quoted
unmapped step, and a single-quoted one — each reddening the suite and naming the offender with
the 300s it would have silently inherited. Each probe reverted.

**Review cost (meter).** 3 rounds, 2 MAJORs. **583,613 codex-reported tokens**
(r1 285,030 / r2 194,264 / r3 104,319).
