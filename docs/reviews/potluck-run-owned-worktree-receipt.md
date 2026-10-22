# Each run gets its own worktree — receipt (SHIP)

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: e0c022620f0ef4550269b4217e1181968b973e36
verdict: SHIP
date: 2026-10-22
findings: docs/reviews/potluck-run-owned-worktree-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) ≠ doer (Anthropic). **Range identity.**
`reviewed_head` is the content tip; `main` at `f8edcf2`.

## What lands, and why it was needed

potluck moved the operator's branch ref — absorbing a doer commit with `reset --soft`, and
rewinding with `reset --hard` on abandonment. A reproduced case destroyed a foreign commit and
its file outright, and a second session was writing in a shared clone of this fleet the same day.
Two attempts to GUARD it failed: ancestry does not prove ownership, and a foreign commit landing
inside the doer window is indistinguishable from the doer's own in a shared checkout under one
user and one git identity. A panel ruled unanimously: **never move a ref the run does not
exclusively control.**

Each run now executes in its own detached worktree. The operator's branch is never moved on any
path. Written by potluck's doer, which decided two things the spec deliberately left open:

- **Placement** — a hidden SIBLING of the operator's checkout, because the fleet resolves sibling
  repos by relative path (`../harness_core`), and anywhere else makes the gate fail as a
  toolchain error that looks like the run's fault. Refuses, naming that cause, if the parent is
  not writable.
- **Output on success** — `git apply` into the operator's working tree as uncommitted change,
  preserving potluck's existing contract and moving no ref.

## Nine review rounds: 6 BLOCKING, 1 MAJOR, 1 MINOR

Recorded in full in the findings file. The shape matters more than the list: **every finding
after the first was about orphaned DIRECTORIES, which cost no correctness** — while the property
the slice exists for was delivered by the first commit and green throughout.

Three of those findings were defects I introduced while fixing the previous one. One was a
warning that promised a sweep I had just deleted. One was a sweep that raced a concurrent run and
would have destroyed a live worktree — worse than the orphan it cleared.

**A5 was AMENDED, not gap-declared** (codex, governance `unanswered_fork`). As written — "no
orphan on any path" — it is unachievable by any implementation, since SIGKILL bypasses every
`finally`. Declaring a gap against an unmet criterion leaves the record saying the work failed
its own acceptance. The amended criterion is removal on every path the PROCESS CAN OBSERVE, plus
an honest report when incomplete, with the hard-kill case declared with its blast radius: it
costs disk and tidiness, never correctness.

## Canon banked

- When a guard's correctness depends on a fact you cannot establish, delete the operation.
- To test a window you must ENTER it; an assertion made after it closed describes a state both
  the fixed and broken orderings reach.
- A fix that special-cases its own failure path deserves the same suspicion as the bug.
- When consecutive fixes each produce the next finding, the SCOPE is wrong, not the fix.
- When a criterion cannot be met by any implementation, amend the criterion in writing.
- Unverifiable is not absent.
- `except Exception` is a claim about which failures you expect — Ctrl-C was the one not covered.

## Verification, outside the loop

`./.claude/verify.sh` ALL SUITES PASS (624). The operator's branch is never moved; a foreign
commit made during a run survives abandonment with its ref, HEAD and `git status` untouched;
the worktree is removed on every observable path; the sibling-path trap refuses by name. Each new
test was probed by reverting its fix — three were rewritten or deleted for passing by
construction, and that is recorded in the commits rather than tidied away.

**Review cost (meter).** 10 rounds, **~1.1M codex-reported tokens**.
