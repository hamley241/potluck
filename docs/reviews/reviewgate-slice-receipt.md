# reviewgate slice — review receipt

The machine-checkable block below is what the reviewgate pre-push hook validates.
This is the FIRST receipt the gate ever checks — its own landing. It authorizes
the reviewgate slice commit in this repo: the codex SHIP pass (full record in
`docs/reviews/reviewgate-code-review.md`) over the change whose tip's parent is
named below.

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: bb3a74a3f3c1a61e4ef2d8bf87b81c6cb9f31ecc
verdict: SHIP
date: 2026-07-21
findings: docs/reviews/reviewgate-code-review.md
```

Reviewer (codex/OpenAI) is a different provider than the doer (Claude/Anthropic),
per G4. Verdict is SHIP-class, per G5. `reviewed_head` names this commit's parent
(the parent form, §A.7c). The verbatim findings across all five review rounds are
in the same-repo pointer above.
