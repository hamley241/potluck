# Slice: the station IMPORTS an attested result — part 2 of 3

**Authority: panel ruling, `conductor/docs/decisions/001-fix-round-delivery-boundary.md`.** Where this
spec and the ruling disagree, the ruling wins and the disagreement is a finding.

**Part 1 (export) landed as PR #19, merge `6278131`**, receipt
`docs/reviews/export-slice-receipt.md`, gate 722. Its `harness/export.py` already gives you
`load_export()`, which is fail-closed on the manifest's shape, every payload digest, and agreement
between the manifest and the `station-run.json` it ships. **Use it; do not re-implement its checks.**

**This slice is potluck-only.** Part 3 (delivery) is the next slice. Keep it that way: scope was the
finding that killed the first attempt at this contract, which escalated with one doer call and no
diff.

## Why

The conductor's fix round currently recreates round 0's complete patch over round 0's already
delivered copy. Part 1 stopped a round from delivering at all; this slice lets the NEXT round start
from the previous round's attested output instead of from committed HEAD. **Both reviewers conceded
the second defect that makes this necessary: even if round 1's delivery had succeeded, round 1's doer
and gate never saw round 0's work — the run gates a tree that is not the candidate.**

## What this slice builds

**Import.** A run told to import takes a previously exported result directory and, before the doer
runs:

1. **accepts the export with `load_export()`** — fail-closed, unchanged;
2. **verifies the cumulative patch against the digest the manifest declares, before applying it**
   (`load_export` already does this for every payload — rely on it rather than repeating it, and say
   in a comment that this is where that check lives);
3. creates the worktree as today — fresh, detached, at the operator's committed HEAD — and
   **applies the patch there**, all-or-nothing;
4. **recomputes the resulting tree's digest and refuses, fail-closed, unless it equals the imported
   result's attested `output_tree_digest`**, naming both digests;
5. only then dispatches the doer.

The point is not convenience. It makes round r+1's input *definitionally* round r's gate-passed,
review-targeted output, and it catches a corrupted or hand-edited export **before any model call is
spent on it**. A test must assert the doer was **not** called when a check refuses.

**Chaining.** When an importing run exports, its manifest's `imported_result_digest` is the **manifest
digest of the result it imported**, not `null`. Part 1 shipped that field as an explicit absence
precisely so this slice fills it rather than reshaping the manifest. The chain must be walkable: from
round r's export you can name round r-1's.

**Restore and resume understand the imported input.** This is the warning the ruling puts above the
design: **do not implement import by disabling restoration.** A retry or a `--resume` of an importing
run restores the **digest-verified imported state**, not an empty base at committed HEAD. A resume
that silently discards the previous round's work is the defect this whole contract exists to remove,
reintroduced one layer down. Part 1 refused `--export-dir` with `--resume` as an undeclared gap; this
slice must either make resume work for an importing run or **refuse it by name**, and say which it
chose and why.

## What must not change

* **Ordinary callers are untouched.** No import flag means today's behaviour exactly, delivery
  included.
* **`head != base` stays a hard refusal.** Importing changes files, not HEAD; the doer still has no
  reason to commit.
* **`station-run.json` stays v1**, and `base_commit` remains the run's **starting HEAD** — a real
  commit, because the landing verifier binds it to the landed commit's parent. The imported state is
  a separate, digest-verified **input**, never a renamed base. **Do not** record the imported tree as
  `base_commit`, and do not hide the imported change behind a synthetic baseline commit: the ruling
  calls that a redesign of the review and provenance contract.
* **`output_tree_digest` keeps meaning the digest of the run's FULL final tree**, so round r's
  attestation covers the whole candidate, not the increment.
* **`--allow-dirty`'s semantics and help text are untouched.**
* **No worktree is intentionally retained**, and the export directory is never written to by an
  importing run — it is an input.
* **One repository.** potluck only.

## Deliberately NOT in this slice

Delivery by manifest digest, the clean-baseline check, the structured refusal, the delivery-failure
message repair, and the `mkstemp` fallback's removal. All part 3. A run that does not import keeps
today's delivery behaviour.

## Review budget

**3 rounds, declared.** If exceeded, the overrun is declared in the receipt.
