# The delivery slice — receipt (SHIP)

```reviewgate
reviewer.model: gpt-6.1-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 9553e0965aeb60083e61ceb0d467d2a52c35dee6
verdict: SHIP
date: 2026-10-24
findings: docs/reviews/deliver-slice-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) != doer (Anthropic).

**BUDGET OVERRUN, DECLARED.** The spec declared **3 rounds**; this took **4**. Round 3 was a SHIP
with a non-blocking note, which codex offered as a declared follow-up. I folded it instead and spent a
fourth round reviewing only that fold, because **a receipt's `reviewed_head` must name content that
was actually reviewed** — a post-SHIP wording commit would have made the binding false. Round 4
SHIPped and printed the same `reviewed_head` independently. The cap terminates findings-chasing, not
verification; the overrun is recorded here rather than left for a reader to notice.

**One non-blocking finding from round 4 is FILED, not folded:**
`FU-partial-application-label-overclaims` in `docs/reviews/potluck-followups.md` — the refusal
*label* still asserts "git apply wrote some files" on the UNKNOWN path, one line above the footer that
was fixed to stop doing exactly that, and the new test checks the footer but not the header. I had
told the reviewer that anything further and non-blocking would be filed. Fixing a one-line string
because it looks cheap is the drift a declared cap exists to prevent.

**Reviewer model, verified per round** from `~/.codex/sessions/.../rollout-*-<thread_id>.jsonl`,
because `codex exec --json` does not emit it.

**Range identity.** `reviewed_head` is the final content tip; this receipt commits over it. The
authorized range is the four content commits on `deliver-by-digest`.

## What this completes

**Potluck's half of the ruling is done.** `potluck deliver --export-dir <dir> --manifest-digest
<digest>` applies a result **once**, with no doer, gate or review, behind selection, identity, the
three-part baseline, result verification and an ignored-path check — all before anything is written.
Part 1 exported, part 2 imported, part 3 delivers. The conductor's switch to this interface is the
next slice, in another repo.

## What review caught that the gate could not

1. **`git apply`'s write phase is not transactional** (r1 MAJOR). Every nonzero exit was reported as
   `patch_conflict` with the assurance *"wrote nothing"*, which can be **false**: git checks
   applicability before writing, then removes and recreates files sequentially and returns on a write
   error without rolling earlier ones back. Reproduced with the fixture codex described — two tracked
   files, the second in a readable but non-writable directory, every baseline check passing because a
   directory's mode is not in the tree — and the operator is told nothing was written while `a.txt`
   holds the partially applied content. A failed apply now **re-verifies** instead of asserting.
2. **The recovery could have destroyed operator data** (r2 MAJOR). I justified `clean -fd` after a
   partial apply on the grounds that the baseline proved there were no untracked-non-ignored files.
   That premise holds **only under the original ignore rules**: a partial apply that modifies
   `.gitignore`, plus a failed checkout restore, would have let the clean run under half-applied rules
   and delete files that were ignored at baseline — `.env`, `.venv`, the footgun `real_restore_tree`
   refuses `-x` to avoid, and they need not overlap any patch path. The cleanup is now conditional on
   the restore succeeding, and says when it was skipped and why.
3. **"Byte-identical" was not** (r1 MINOR). The preservation assertions compared `git ls-files -s` —
   entries, not the stat cache or index extensions — so a metadata-only index rewrite would have
   passed. They now hash the real index file, read before any other git command, and the shared test
   helper runs `GIT_OPTIONAL_LOCKS=0` so an observation cannot refresh what it observes.
4. **The footer claimed more than it knew** (r3, folded in the overrun). `partial_application` covers
   both a demonstrable write and a **failed re-verification**, where the state is UNKNOWN; the footer
   asserted "Files WERE written" on both.

## Two things I got wrong, and how they were caught

**A fixture that passed for the wrong reason.** My first test for the conditional cleanup saw the
ignored file survive and looked green. The probe showed `restore: ok` — `checkout-index` had
**succeeded**, because `git apply` unlinks and recreates (needing directory write) while
`checkout-index` writes through an existing file, and it restored `.gitignore` before reaching the
unwritable path, so the clean ran under the original rules. **The dangerous ordering is unreachable
with directory permissions alone.** That is recorded in the test, and the branch is covered by
injecting the failure instead. codex confirmed the injected state is reachable in reality — an
interruption or filesystem write error can stop restoration before `.gitignore` is restored.

**Two criteria of mine that contradicted each other.** A16/A17 demanded the `mkstemp` fallback and
the *"changed during the run"* message be removed from "the delivery path", while A3 demanded ordinary
callers be untouched. The run's own reviewer raised that as a MAJOR; the doer rejected it with grep
evidence; the internal reviewer conceded. I verified each claim — one caller of `.deliver()`, in the
`else` branch, and `--import-dir` now requires `--export-dir` — and **amended both criteria in
writing**, because for a plain run the message is accurate and the temp copy is the only copy. codex
judged the narrowing legitimate, with the qualification that application failure alone does not prove
the tree changed (filesystem errors also cause it) — inherited behaviour A3 protects.

**Verification not claimed.** codex ran no suite in any round. Gate figures are the doer's: 793 on
`main`, 869 after the build, 876 after r1's fold, 886 after r2's, **890** after the overrun fold, all
`ALL SUITES PASS` on `UV_PYTHON=3.11`. The decisive negatives were each checked by reverting their fix
and **reading the whole output** — twice today a grep's silence nearly passed for a green test, and
once I wrote a gate figure (894) before reading it and amended the commit to the 890 I had read.

**Review cost (meter).** r1: 382,650 input (317,568 cached), 2,450 output. r2: 116,067 (91,520),
1,155. r3: 108,522 (92,672), 818. r4: 100,824 (91,776), 536.
