# Acceptance: the station DELIVERS a selected result — part 3 of 3

Graded against `deliver-by-digest-spec.md` and the panel ruling it cites. Every criterion was checked
for **reachability on this checkout** before being written; where one was proved reachable by
experiment, that is said. **If a criterion proves unreachable or wrong, amend it in writing here and
say why.**

## The gate

**A1.** `./test` exits 0 with `ALL SUITES PASS`, with **at least** the 793 checks `main` has today.
Not an exact number: this slice adds tests.

**A2.** Each negative case is a constructed failure fixture required to fail **and to name the
reason**. A check that raises instead of reporting is itself a defect — part 2 fixed one of those.

## Ordinary callers

**A3.** A `potluck fix` with no import, no export and no delivery flag behaves exactly as today,
delivering on PASSED and writing nothing otherwise.

**A4.** `--allow-dirty`'s help text and its three effects remain unchanged, existing source-level pin
still passing.

## Selection

**A5.** Delivery selects a result **by manifest digest**. A digest that does not match the named
directory's manifest refuses, naming both. **A pathname alone never authorizes delivery.**

**A6.** Delivery runs **no doer, no gate and no review** — a test fails if any model call is
dispatched.

## The baseline, all three parts

**A7.** Refused unless operator `HEAD` equals `manifest.base_commit`, naming both commits. Fixture:
an empty commit on top, which changes no tree.

**A8.** Refused unless the **real index** represents `base_commit`'s tree, checked without refreshing
or rewriting it. Fixture (**proved reachable**): stage a modification, then restore the file's
content — the `add -A` digest then equals `base_commit^{tree}`'s while `git diff --cached HEAD`
reports `M`.

**A9.** Refused unless the operator working tree digest equals `base_commit^{tree}`'s, on the digest
boundary rather than `git status`. **Fixture that matters most: a foreign edit to an unrelated file,
where the patch WOULD apply cleanly** — delivery must still refuse, because patch applicability is
not whole-tree equality, which is the argument that decided the ruling.

**A10.** An **ignored-path obstruction** that application would overwrite is refused rather than
clobbered.

## Identity

**A11.** Refused on a `push_url_raw` mismatch and on a `root_commits` mismatch, naming both sides.

**A12.** A manifest whose `push_url_raw` is `null` is **refused by name**, fail-closed.

**A13.** `repository.name` is **never** compared — a renamed checkout still delivers. Asserted by a
fixture that renames the directory.

## Application and refusal

**A14.** Application is **all-or-nothing** and writes **no ref**; the real index is not used as
scratch.

**A15.** On any refusal, operator `HEAD`, every ref, the real index and the working tree are
**byte-identical to pre-invocation**, and the selected result is **retained**. No rebase, no
overwrite.

**A16. AMENDED — scoped to the digest-bound delivery operation.** *As written this conflicted with
A3, and the conflict was mine.* The refusal of `potluck deliver` states captured
**expected-vs-observed** facts, **distinguishes baseline drift from patch conflict**, and names
**where the retained result is**, in the operator-facing message. `_RunWorktree.deliver()` keeps its
existing message, because **there it is accurate**: that path is reachable only for a plain run with
no import and no export (verified — one caller, in the `else` branch of `if export_dir is not None`,
and `--import-dir` now requires `--export-dir`), and a plain run's base is the HEAD captured when the
run started, so a failed application means the tree really did change *during* the run. The message is
wrong only in a fix round, and a fix round now always exports and is delivered by digest.

**A17. AMENDED — scoped the same way.** The `mkstemp` fallback is gone from the **digest-bound
delivery path**, where the retained export *is* the copy. It **stays** in
`_RunWorktree.deliver()`, because a plain run has **no export to retain the patch in**, so removing
it would discard a passed run's work — which A3 forbids. A test asserts the new path's refusal names a
durable path rather than a temp file.

*Provenance of this amendment:* the run's own reviewer raised the unamended criteria as a MAJOR, the
doer rejected it with the grep evidence above, and the reviewer conceded (`held_issues: []`). The
doer argued it in the review log but did not amend here, so I did, having verified each claim against
the code. Two criteria of mine that contradict each other for one code path is the same unchecked
absolute as before, wearing a different coat.

## End to end

**A18.** Round 0 exports; round 1 imports and exports; delivering **round 1's digest** lands the
complete candidate — both rounds' work — **once**, with the operator tree matching round 1's attested
`output_tree_digest` afterwards.

**A19.** `--import-dir` without `--export-dir` is refused at argument validation, naming both flags
and the recovery, before any doer call.

**A20.** Part 2's `test_retry_restores_imported_state` is **amended in writing** where it asserted
direct delivery of an importing run, with the reason recorded in the test itself.

**A21.** The contract states plainly that "once" is a **digest-bound application with a baseline
refusal**, and that durable exactly-once acceptance across retries, crashes, empty patches or two
results for one task is the caller's, not potluck's.

## Boundaries

**A22. `station-run.json` stays v1**: field set and descriptions unchanged, no new required field.
**If the slice cannot be built without changing v1, it stops and says so.**

**A23. No worktree is intentionally retained**; after every path no `.<repo>.potluck-run-*` directory
or `.git/worktrees` entry for it remains.

**A24. One repository.** No file outside this checkout is modified, verified after the run against
every sibling repo's recorded HEAD.
