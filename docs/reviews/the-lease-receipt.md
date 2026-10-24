# The lease — receipt

Authorizes ruling 002 R3 and R5's emission (conductor `docs/decisions/002-early-signal.md`):
one attempt per admitted call, `doer:implement` at 1200s, the restated `feature_seconds`, the retry
policy in the committed identity, and the implement prompt's `prompt_size` event.

```reviewgate
reviewer.model: gpt-6.1-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: d7458e0c8d345c3ff8695ae8b0594a80c523220b
verdict: SHIP
date: 2026-10-23
findings: docs/reviews/the-lease-code-review.md
```

**Built by potluck** (run `the-lease`: doer claude, reviewer codex, 0 fix rounds, reviewer found no
issues), spec 61 lines / acceptance 16 criteria committed before the run at `653e7df`. One hand
fold afterwards: codex r1's MINOR, the README's control guarantee.

**The run was its own evidence.** It ran under the lease it replaced: attempt 1 of `doer:implement`
was killed at 598s with 1 edit, the tree was restored, and attempt 2 succeeded from scratch at 516s
(`~/.claude/projects/*potluck-run-up3p6ndh/`). The transparent retry has now rescued **2 of 4** runs
whose first attempt was killed. That is why R3 removes it in favour of one 1200s attempt — the same
implement allowance, without discarding the first 600s of work — and not because a retry never
helps; the receipt says so rather than overstate the ruling.

**What this does not claim.** `calls_dispatched` counts bounded operations (gates and diffs too),
each now exactly one attempt; it is not a provider-call counter (ruling R3, deferred). 1200s is a
justified experiment, not a proven sufficiency — completion times were censored at 600s.

**Gate.** `./test` exit 0, `ALL SUITES PASS`, **910** `[PASS]` (890 on `main`), under
`UV_PYTHON=3.11` on the operator checkout. Siblings menu, menu-rp, conductor, harness_core unmoved
and clean; no `.potluck.potluck-run-*` residue.

**Budget:** declared 2 rounds, spent 2.
