# The import slice — receipt (SHIP)

```reviewgate
reviewer.model: gpt-6.1-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 53631a00cd6ab539ae23339853da86f4b6ad2420
verdict: SHIP
date: 2026-10-23
findings: docs/reviews/import-slice-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) != doer (Anthropic). **SHIP at round 2 of a cap of
3 declared in the spec before the run.** 2 MAJOR at round 1, both folded and both reproduced before
fixing. **One non-BLOCKING finding is DECLARED, not folded:** `FU-nofollow-temp-writes` in
`docs/reviews/potluck-followups.md`, which this commit also adds — a link installed *after* the
preflight scan can still be followed by ordinary temporary-file writes, and the remedy is to make
every write into a caller-supplied directory no-follow rather than rely on a point-in-time scan.
codex named it non-BLOCKING when asked to choose between a finding and a declared follow-up.

**Reviewer model, verified.** `gpt-6.1-sol`, read from the CLI's session rollout; `codex exec --json`
does not emit it. Earlier fleet receipts name `gpt-5.6-sol` and were correct when written.

**Range identity.** `reviewed_head` is the fold commit; this receipt commits over it. The authorized
range is the two content commits on `import-attested-seed` (`3538fbb` build, `53631a0` fold).

## PROVENANCE — this slice is part recovery and part reconstruction

The build came from a `potluck fix` run that PASSED with one fix round. **I then destroyed the
delivered working tree** with an over-broad `git checkout --` while reverting a change of my own that
should never have been made. The record, stated plainly because a receipt that hides this is worth
nothing:

* `harness/export.py` and `harness/test_import.py` were never checked out — **the doer's work,
  untouched**.
* `harness/cli.py` and the spec were recovered from the run-dir checkpoint's `state.diff`, which
  holds the **pre-fix-round** diff (32,893 bytes, matching `apply_fixes_diff`'s `pre_bytes`). **The
  post-fix diff is stored nowhere** — only its size, 38,975 bytes.
* The fix round's two accepted findings — the output/import overlap refusal and the `base_commit`
  check — and their two spec paragraphs are **my hand reconstruction** from the run's recorded
  `review` and `doer_response` events.

The reconstruction's independent check was the doer's unmodified `test_import.py`, which tests
exactly those behaviours and passes. codex was told all of this and asked to treat `cli.py` as code
whose author is less trustworthy than usual, and to report if those tests were weaker than the
behaviours they are named for. It found no such weakness — and then found two MAJORs that neither the
doer's review nor the tests had caught.

**A retraction, recorded.** I probed `--import-dir` without `--export-dir`, saw both rounds' files
land in the operator tree, and called it the cumulative-redelivery defect. It is not: round 0
*exported* and delivered nothing, so round 1 delivering the complete candidate once is correct — the
defect requires a prior delivery to apply over. The doer's suite refuted me by failing, and one of its
tests is named *delivery carries both rounds' work*. codex confirmed the retraction and asked part 3
to define that combination's delivery behaviour explicitly.

## What the review caught that neither the gate nor the run's own review did

1. **The applied patch bytes were not the verified bytes.** `load_export` verifies every payload, but
   `_load_import` then re-read `cumulative.patch`, so a substitution between the two reads was applied
   unverified. The tree-digest argument sitting there did **not** close it: a substituted patch can
   produce the attested tree **plus an ignored file**, which `_output_tree_digest` excludes by design
   while the doer and the gate consume it — and `import_restore` would reapply those bytes on every
   retry. With the check removed, the doer demonstrably runs on the substituted patch.
2. **A symlink reached the import through disjoint paths.** `_overlaps` compares paths, so a
   pre-created `run-dir/imported-result.json -> <import>/station-run.json` passed it and the marker
   write followed the link, truncating the accepted input artifact before the doer ran. With the fold
   reverted, `load_export` refuses the export afterwards because `station-run.json` no longer matches
   its manifest digest — the accepted input is destroyed — and the doer still runs.

Both now have tests that **fail when their fix is reverted**, which I verified by reading the
recorded failures rather than inferring them from a silent grep. One of those tests was also changed
to **report** instead of raising: it called `load_export` directly, so without the fold it aborted the
suite instead of recording the failure it exists to catch.

**Verification not claimed.** codex ran no suite in either round. Gate figures are the doer's: 722 on
`main`, 786 after the build and reconstruction, **793** after the fold, all `ALL SUITES PASS` on
`UV_PYTHON=3.11`.

**Review cost (meter).** Round 1: 404,623 input (356,096 cached), 2,860 output. Round 2: 150,740
input (125,184 cached), 1,117 output.
