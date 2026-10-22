# A failed run says what the doer said, and where the restore reached — receipt (SHIP-at-cap)

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 1717ddd1548dc96a4fe2f925fe2be897809f0ff2
verdict: SHIP
date: 2026-10-22
findings: docs/reviews/potluck-run-says-what-it-did-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) ≠ doer (Anthropic). **Range identity.**
`reviewed_head` is the content tip; `main` at `96edc60`.

**Verdict basis, stated honestly.** This is a SHIP-AT-CAP, not a zero-findings SHIP. The review
budget was fixed at THREE rounds before it began, after the previous slice ran to ten; at round
three the remaining findings were resolved by amending an over-specified criterion, and nothing
was left that the reviewer said must not ship.

## What lands

Two declared follow-ups closed as one change, because both are the same property: a run that
ends badly should be diagnosable from its artifacts without re-running anything.

- **The doer's reply on a no-change run** is captured and readable back. It was discarded, so
  `escalated_no_signal` reported "produced no changes" and destroyed the only evidence of WHY.
- **The restore's scope is stated** — what it covers and, in the same breath, that edits outside
  it are neither restored nor detected. Nothing claims to detect a foreign-repo edit; the run
  cannot enumerate repos it was never told about.

**Written by potluck, which PASSED at 1 round** — the session's first clean full-loop pass, gate
green and the in-loop reviewer satisfied. It also exercised the worktree slice from `96edc60`:
the work arrived as uncommitted change, HEAD never moved, no worktree survived.

## The design question the doer answered better than the spec asked

The spec said only "think about where the reply is written and who can read it, and say what you
decided". The doer put the full text in the `--log-file` ALONE and chmods it **0o600**, because
the reply is model output that can quote the operator's source. stdout and the debate log get a
capped preview.

## Eight findings across three rounds, zero BLOCKING

The MAJOR worth naming: `restore-scope-wrong-path` — the scope statement named the OPERATOR'S
CHECKOUT as what the restore covers, which is the one tree it does not touch. A function written
to end claims-coverage-it-lacks, making exactly that claim. **Its test pinned the defect**,
asserting the checkout must be named as covered; it would have defended the bug against the fix.

Also folded: `--allow-dirty` disables restore entirely and the scope still claimed coverage;
recovery depended on an optional flag; an unbounded write could crash the declared exit; a
stale reply could attach to an unrelated run on a reused Orchestrator; and the warnings that
exist so persistence cannot change the exit each ended in a bare `print` that can itself raise.

**I misplaced one fix and caught it by checking** — anchored on a docstring, it landed in
`_capture_diff` instead of `run_feature`, where it would have wiped the reply it was meant to
protect. The gate was green either way.

## A1 AMENDED, not quietly shipped against

As written — verbatim from "the run's artifacts" — it cannot hold when no durable artifact was
requested, nor for an unbounded reply against A3's "capture changes no outcome". Amended:
verbatim up to 256 KiB when `--log-file` or `--run-dir` was given, truncation and true length
recorded; otherwise a stated stderr note, labelled disclosure rather than recovery.

**Second time today a review ended by indicting a criterion I wrote.** The pattern: I specify an
absolute, the implementation discovers the qualifier. Writing the qualifier first is cheaper.

## Verification outside the loop

Gate **648**. The reply reads back VERBATIM and neutering capture fails that assertion
(`len=0 want=83`) rather than passing by construction; empty, text, long and oversized are
separate cases; the truncation boundary is pinned.

**Review cost.** 3 rounds, ~290k codex-reported tokens — against ~1.1M for the ten-round slice.
