# `--resume` preserves ignored files — receipt (SHIP)

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 3ba57448b44a57ee32f506097d6005cc0b5a39b3
verdict: SHIP
date: 2026-10-22
findings: docs/reviews/potluck-resume-ignored-files-code-review.md
```

**Provider independence (G4/G5).** codex (OpenAI) ≠ doer (Anthropic). SHIP-class, zero findings,
one round. **Range identity.** `reviewed_head` is the content tip; `main` at `f3560ab`.

## What lands

`--resume` was unusable in **every repo in this fleet**. The resumability work the
first-customer report called "the headline fix" could not run on the fleet that owns it:
`menu` carries `.venv/` plus five `runs/` directories, `potluck` and `harness_core` carry
`.venv/`, `conductor` carries `__pycache__/`, and every resume refused with *"ignored files
are present that `clean -fdx` would delete"*.

The cause was two sibling restore paths ruling OPPOSITE ways on `git clean -x`.
`real_restore_tree` preserves ignored files with a written rationale — *"-x is deliberately NOT
used here: it would delete .venv, node_modules, and .env"*. `restore_tree` deleted them, so it
had to refuse up front, on an assumption stated in its own comment: *"(Mock phase-1 repos carry
none, per obs-017.)"* True of the derma mock, false of everything here.

The resume path now follows its sibling's ruling. No `-x` remains anywhere on it, the
ignored-file refusal is gone, and the code states what the restore does and does not guarantee
about ignored paths rather than leaving the new boundary to be inferred.

**Two changes beyond the written spec, both consequential and both reviewer-endorsed.**
`cli.py`'s comment and error message and `schemas.py`'s field description still said
`clean -fdx` and "tracked+untracked+ignored". Removing `-x` made those statements FALSE.
Leaving them would have been the same "document describes the design, not the wiring" defect
this fleet folded twice earlier the same day.

**An edge case the doer found and the spec did not name:** an ignored path sitting exactly where
a snapshot entry must go. It fails rather than removing the path — consistent with the
no-data-loss constraint.

## Written by potluck. The run reported that nothing happened.

The doer wrote this and **committed** it. potluck's no-change detection reads the working tree,
which was therefore clean, so the run reported `escalated_no_signal` /
*"doer:implement produced no changes"* and **ran no gate at all**. The work was found by
inspecting the branch, not by the loop reporting it.

That is filed as `FU-doer-commit-defeats-detection`, and the rollback half is worse than the
mis-report: `real_restore_tree` runs `git reset --hard` with no ref, so a moved HEAD means the
"restore to baseline" resets to the doer's own commit and **cements** the work it was meant to
discard. An escalated run is therefore not safely abandonable.

Consequently this diff had **no in-loop gate and no in-loop review**. The gate was run by hand
(ALL SUITES PASS, 601) and the codex round below is the only independent check it received.

## Verification

- `./.claude/verify.sh` → **ALL SUITES PASS (601)**.
- **A4** — no `-x` remains anywhere in `checkpoint.py`.
- **A5** — `real_restore_tree` byte-unchanged; `orchestrator.py` not in the diff.
- **A7** — the new test resumes in a repo carrying `.venv/` and `.env`, with the venv file
  written as BINARY content including a NUL and `\xff`, and asserts the bytes survive intact.
  That shape also catches text-mode corruption, which the spec did not ask for.

**Review cost (meter).** 1 round, 0 findings, **102,177 codex-reported tokens**.
