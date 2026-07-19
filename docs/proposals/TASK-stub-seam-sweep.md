# TASK — sweep the stub seam across all three repos

**Status:** RULED 2026-07-19, not started. The ninth sibling sweep, and the
first aimed at a STRUCTURAL property rather than a bug's twin.

**Origin:** observation 008. `real_run_gate` shipped broken to origin under
a fully green suite because it had zero executions — every orchestrator test
injects a stub gate callable. That is not one missing test; it is the shape
of the stub strategy, which is otherwise correct and stays.

## The rule

Every `real_*` function and every real client carries **at least one no-mock
execution test covering its outcome classes.**

Where a boundary genuinely cannot run without a model, the testable PARTS
are still executed — argv construction, output parsing, file I/O, exit-code
handling — and the untestable remainder is NAMED in the test. That is
scope-of-claim applied to the suite's own architecture: a test that cannot
cover the whole boundary says so, rather than implying it did.

## The enumeration (produce it mechanically; do not take it from here)

Start with `grep -rn "^async def real_\|^def real_\|class Real"` across all
three repos, then rule on each. Known at ruling time:

**potluck**
  * `real_run_gate` — DONE (`harness/test_real_gate.py`, four outcome
    classes, nine assertions, SA-7 verified against the broken form).
  * `real_get_diff` — untested at the boundary, and it has the richest
    behaviour of any of them. Outcome classes worth covering: tracked-only
    changes; untracked files included; the size and binary skip-markers
    actually firing; a `git ls-files` failure raising; `git diff --no-index`
    exit 1 (differs, legitimate) vs >1 (real error → visible marker).
  * `real_restore_tree` — has a temp-repo test; confirm it covers the
    FAILURE path (a git that exits non-zero), not only the happy one.
  * `RealDoerClient` — model-bound. Testable parts: argv/flag construction
    per mode, prompt assembly, and the parse boundary on canned stdout.
    Name the model call as out of scope in the test itself.

**menu**
  * Its real clients' non-model machinery: argv construction, output
    parsing, file I/O. Same split — parts executed, remainder named.

**harness_core**
  * `run_subprocess` / `run_subprocess_result` — the teardown paths have
    tests; confirm the RESULT paths (exit codes, decode with
    `errors="replace"`, stderr excerpting) are executed too.

## Verification

SA-7 both directions with probe evidence for every check added: each probe
prints the resolved module path it exercised, so "failed against the
reverted tree" is evidenced rather than assumed.

## Why it is a sweep and not a backlog item

Findings this shape have never lived in one repo. Eight consecutive sibling
confirmations preceded this one; the difference here is that the sibling is
not a copy of a bug but the same architectural decision made in three
places — so the blind spot exists wherever the decision does, by
construction rather than by coincidence.
