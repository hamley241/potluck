# The export slice — receipt (SHIP)

```reviewgate
reviewer.model: gpt-6.1-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 26af2857442d8832702278751abacbd2abd6fee4
verdict: SHIP
date: 2026-10-23
findings: docs/reviews/export-slice-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) != doer (Anthropic). **SHIP at round 3 of a cap of
3 declared in the spec before the run** — 3 MAJOR + 1 MINOR at round 1, 1 MINOR regression at round 2,
all folded, **no overrun to declare and no finding deferred**. At the cap round codex was asked to
name any non-BLOCKING change as a declared follow-up rather than spend a round outside the budget; it
named none.

**Reviewer model, verified per round.** `gpt-6.1-sol`, read from the CLI's session rollout for each of
the three review threads, because `codex exec --json` does not emit it. Earlier fleet receipts name
`gpt-5.6-sol` and were correct when written — the default moved during 2026-10-23.

**Range identity.** `reviewed_head` is the single content commit; this receipt commits over it.
Rounds 1 and 2 reviewed that content as an uncommitted working tree, round 3 reviewed it as the
commit named above.

**Inputs preserved.** `docs/slices/export-import-deliver-spec.md` and `-acceptance.md` were committed
**before** the run, and the narrowing commit records why the scope changed. **The doer amended two
acceptance criteria in writing**, as the acceptance invites; codex judged both improvements rather
than evasions — A6 restored the task identity the ruling requires and my spec had dropped, and A11
made export failure observable while keeping the station's verdict separate.

**Scope, honestly.** This is **part 1 of 3** of the ruling's potluck half: export only. Import,
delivery, the delivery-failure message repair and the `mkstemp` fallback's removal are parts 2 and 3
and are explicitly out of scope here. codex confirmed nothing in this slice forecloses them. The
first attempt at the whole half in one slice escalated `escalated_timeout` at `doer:implement` with
one call and no diff; the scope was the finding, not the payload.

**What the review caught that the gate could not.** `_write_atomic` used `Path.write_bytes`, which
**follows symlinks** — so a planted `cumulative.patch.tmp` pointing into the operator's checkout was
written **through**, truncating a tracked operator file from the one code path whose entire purpose is
to touch nothing outside the export directory, **while still exiting 0**. I reproduced it before
fixing it: with `write_bytes` restored, the fixture shows `base.txt` overwritten by patch bytes and
the run reporting success. Validating the export directory could never have caught this — the
directory is legitimately outside the tree; it was the link inside it that escaped.

**Verification not claimed.** codex ran no suite in any round. Every gate figure here was measured by
the doer: 648 on `main`, 696 after the build, 718 after the r1 fold, **722 after the r2 fold**, all
`ALL SUITES PASS` on `UV_PYTHON=3.11`. Each new test was also checked to **fail when its fix is
reverted** — the symlink fixture and the uncaught-`ExportError` fixture were both reverted and
observed failing, the second by reading the traceback rather than inferring it from a silent grep.

**Review cost (meter).** Round 1: 242,779 input (207,488 cached), 2,312 output. Round 2: 165,917
input (138,752 cached), 1,548 output. Round 3: 114,215 input (87,040 cached), 491 output.
