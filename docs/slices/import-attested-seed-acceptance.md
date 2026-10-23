# Acceptance: the station IMPORTS an attested result — part 2 of 3

Graded against `import-attested-seed-spec.md` and the panel ruling it cites. Every criterion was
checked for **reachability on this checkout** before being written. **If one proves unreachable or
wrong, amend it in writing here and say why** — an unmet absolute left standing makes the record say
the work failed its own acceptance. Part 1's `harness/export.py` and `harness/test_export.py` are on
`main` and are the fixtures to build from.

## The gate

**A1.** `./test` exits 0 with `ALL SUITES PASS`, with **at least** the 722 checks `main` has today.
Not an exact number: this slice adds tests, so it rises.

**A2.** Each **negative** case is a constructed failure fixture required to fail **and to name the
reason**, not a test asserting a boolean.

## Ordinary callers are untouched

**A3.** A run with no import flag behaves exactly as today, delivery included, proven over a
temporary repository.

**A4.** `--allow-dirty`'s help text and its three effects remain unchanged, with the existing
source-level pin still passing.

## Import

**A5.** A run told to import an export produced by part 1 starts its doer from a worktree whose tree
digest equals the imported result's attested `output_tree_digest`. Asserted on the digest, not on a
file listing.

**A6.** The worktree is still created **fresh, detached, at the operator's committed HEAD**, and the
imported patch is applied **there**. `git rev-parse HEAD` inside the run's worktree still equals the
operator's HEAD.

**A7.** A corrupted export is refused **before any doer call**: the test asserts the doer was not
called. (Constructed: flip one byte of `cumulative.patch`.)

**A8.** An export whose patch applies but whose resulting tree digest does not equal the attested
`output_tree_digest` is refused fail-closed, **naming both digests**. (Constructed: a manifest and
artifact that agree with each other but attest a tree the patch does not produce.)

**A9.** A patch that does not apply to the fresh base is refused, naming the failure, with nothing
written to the operator's tree.

**A10.** An incomplete export — payloads but no manifest — is refused by `load_export`'s existing
named error, not by a new ad-hoc check.

**A11.** The import writes **nothing** to the operator's working tree and **nothing** to the export
directory: both are compared before and after, the operator's by refs, HEAD, index and file bytes.

## Chaining

**A12.** When an importing run exports, the new manifest's `imported_result_digest` is the **manifest
digest** of the result it imported — not `null`, not a payload digest. A two-round fixture walks the
chain from round 1's export back to round 0's.

**A13.** A run that does **not** import still records `imported_result_digest: null`, so part 1's
explicit absence is preserved rather than silently dropped.

## Restore and resume

**A14.** A retry or resume of an importing run restores the **digest-verified imported state**, not
an empty base — **or** `--resume` with an import is refused **by name**, with the refusal explaining
that an importing resume is not built. **Whichever is chosen must be stated in the spec and tested;
an unstated choice is a finding.**

**A15. Import is not implemented by disabling restoration.** A source-level check shows the restore
path remains wired for importing runs, so the ruling's warning cannot be satisfied in appearance
only.

## Boundaries

**A16. `station-run.json` stays v1**: field set and descriptions unchanged, `base_commit` still the
run's **starting HEAD** and a real commit, `output_tree_digest` still the digest of the **full** final
tree. The imported tree is **never** recorded as `base_commit`, and no synthetic baseline commit is
created. **If the slice cannot be built without changing v1, it stops and says so.**

**A17. `head != base` remains a hard refusal** for an importing run too, with its existing reason
text intact.

**A18. No worktree is intentionally retained**, and after every path — pass, refusal, resume — no
`.<repo>.potluck-run-*` directory and no `.git/worktrees` entry for it remains.

**A19. One repository.** No file outside this checkout is modified. Verified after the run against
every sibling repo's recorded HEAD.

## Out of scope — do not do these here

Delivery by manifest digest, the clean-baseline check, the structured refusal, the delivery-failure
message repair, and the `mkstemp` fallback's removal. Part 3.
