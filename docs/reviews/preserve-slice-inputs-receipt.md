# Preserve each slice's spec and acceptance — receipt (SHIP)

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 857f09fa7187ccab47c041b8507e59a192d01191
verdict: SHIP
date: 2026-10-22
findings: docs/reviews/preserve-slice-inputs-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) ≠ doer (Anthropic). Docs only; no module under
`harness/` is touched.

## Why

Every receipt in `docs/reviews` says "written by `potluck fix`" and binds a reviewed head —
while the spec and acceptance those runs were GRADED AGAINST lived only in a session-scoped temp
directory. When the session ends they are gone, and the receipts reference inputs nobody can read.

That is exactly how menu lost its handoff acceptance golden: hand-authored, never committed,
machine-local, lost in a migration. menu's acceptance fidelity is **tier-PROMISED to this day**
because of it. This fleet spent a day writing receipts with the same hole in them.

**Banked: a receipt whose inputs cannot be read is a receipt for work nobody can re-judge.**

## What codex stopped

The commit originally also carried the codex review driver, as "reusable infrastructure".
**BLOCKING, `driver-leaks-machine-absolute-paths`** — it exposes `/Users/gp` paths, a session
temp path, a local checkout name and a session UUID. **MAJOR,
`driver-is-session-bound-not-reusable`** — it hardcodes one repository, one slice, one round
and inputs that no longer exist, so it is scratch machinery, not infrastructure.

Both correct, and both found by the question I asked it to attack. The driver is **not
committed**. Writing a real `conductor review` entry point is its own slice if it is wanted;
calling scratch "infrastructure" because it was useful once is how a repo fills with
machine-specific junk.

## What lands

Eight files: the spec and acceptance for the respond-timeout band, the resume ignored-files fix,
the run-owned worktree, and run-says-what-it-did. Verbatim, as the runs were graded against.
Checked for machine paths and secrets: none — they are prose.
