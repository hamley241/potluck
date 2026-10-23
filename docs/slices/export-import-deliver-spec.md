# Slice: the station EXPORTS — part 1 of 3

**Authority: panel ruling, `conductor/docs/decisions/001-fix-round-delivery-boundary.md`** (codex +
kimi, independently, two rounds of three; kimi conceded; codex SHIP on the record at r2). That
document is the design authority. Where this spec and the ruling disagree, the ruling wins and the
disagreement is a finding.

**This slice is potluck-only.** The conductor's switch to exported results is a later slice in the
conductor's repo, and nothing here may edit that repo.

## Why this spec is narrow — the first attempt is the evidence

The ruling's potluck half was first specified as one slice covering **export, import and delivery**
plus input-aware restore and resume, with 23 acceptance criteria. That run escalated:
`escalated_timeout`, `[doer_no_signal] doer timed out during implement`, `rounds_used: 0`,
`calls_dispatched: 1`, `state.diff: null` — **one** `doer:implement` call, killed at its 600s
timeout, having produced nothing. The payload was never in question; the scope was.

**So the ruling's potluck half ships as three slices, in this order**, because each needs the one
before it to exist:

1. **export** — this slice.
2. **import** — seed a round from a previous round's attested export, digest-verified fail-closed,
   with restore and resume understanding the imported input.
3. **deliver** — an explicit delivery operation selected by manifest digest, with the baseline check,
   the structured refusal, and the repaired failure message.

The contract is unchanged. Only the number of runs is.

## What this slice builds: export, and nothing else

A `potluck fix` run that is told to export **does not apply anything to the operator's working
tree**. On **PASSED** it writes a portable result into a caller-supplied directory that is outside
the target's git tree:

* a **versioned manifest** naming: the task, this run and round (as the ruling requires), the
  repository identity, the **original task
  base commit**, the cumulative patch digest, the output tree identity and digest, and the station
  artifact digest. It also names the **imported-result digest**, which in this slice is always its
  explicit absence — the field exists now so part 2 does not reshape the manifest.
* a **self-contained binary patch from the original task base to the complete output** — additions,
  deletions, exec and symlink modes, binary bytes, and untracked-non-ignored files, matching the
  boundary `snapshot_tree()` already draws. Ignored paths stay out.
* the run's `station-run.json`, and the **exact** spec, acceptance and gate inputs needed to resolve
  its bindings.

Payload filenames are **manifest-relative** and every payload's digest is in the manifest. The
**manifest is published last**: a directory holding payloads but no manifest must not be acceptable
to any later operation. **A PASSED run that cannot complete its export fails** rather than reporting
success — the export is the round's product, and a silently missing one becomes a confusing refusal
a slice later.

A result is addressed by its **manifest digest** and carries **bytes**. Never a bare tree OID: a
`TreeSnapshot`'s `work_tree_oid` is a loose object left unreachable once the worktree is removed, so
it survives only until a `git gc`.

## What must not change

* **Ordinary callers are untouched.** A `potluck fix` invoked as today — no export — still applies
  its change to the operator's tree on PASSED, still writes nothing on any other outcome, still
  removes its worktree on every path.
* **`head != base` stays a hard refusal**, and in this slice it also makes a run **ineligible to
  export**: a worktree whose HEAD left the base holds work that never reached the review diff.
* **`station-run.json` stays v1**, with `base_commit` still the run's starting HEAD and
  `output_tree_digest` still the digest of the run's full final tree. All transport metadata lives in
  the export manifest, a companion artifact. *Silently reinterpreting v1 is unacceptable* — if this
  cannot be built without changing v1, **stop and say so**; the verifier slice comes first, in
  `harness_core`.
* **`--allow-dirty`'s semantics and help text are untouched.** Its help is the one description of
  this mechanism that was never wrong.
* **No worktree is intentionally retained.** The transport is a file.
* **One repository.** potluck only. The restore is single-repo, so an edit outside this checkout
  escapes it with no restore and no record.

## Deliberately NOT in this slice

Import, delivery, the delivery-failure message repair, the death of the `mkstemp` fallback, and any
change to restore or resume. They are parts 2 and 3. Delivery still behaves exactly as it does today
for a run that was not told to export.

## Review budget

**3 rounds, declared.** If exceeded, the overrun is declared in the receipt.
