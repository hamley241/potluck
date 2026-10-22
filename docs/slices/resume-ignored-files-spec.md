# `--resume` refuses in every repo in the fleet

The resumability work the first-customer report called "the headline fix" cannot run on the
fleet that owns it. `potluck fix --resume <run-dir>` fails with:

    resume refused (fail-closed): restore refused: ignored files are present that `clean -fdx`
    would delete during restore (refusing to destroy uncommitted ignored work -- remove them
    explicitly to resume)

Observed live. It fires in all four repos: `menu` carries `.venv/` plus five `runs/` directories,
`potluck` and `harness_core` carry `.venv/`, `conductor` carries `__pycache__/`.

## The cause: two sibling restore paths made OPPOSITE rulings on `-x`

`real_restore_tree` (end of a failed run, `harness/orchestrator.py`) uses `git clean -fd` and
PRESERVES ignored files. Its docstring states the reasoning and it is the right one:

> `-x` is deliberately NOT used here: it would delete `.venv`, `node_modules`, and `.env` —
> destroying local environments and secrets, a far worse data-loss footgun than the hole it
> closes. Ignored paths are already excluded from the review diff by design, so they are out of
> scope for hygiene too.

`restore_tree` (the resume path, `harness/checkpoint.py`) uses `clean -fdx` when it empties the
working tree, so it MUST refuse up front rather than destroy ignored work. Its refusal is
correct given that choice. The choice is what is wrong, and the comment beside it shows why the
mismatch went unnoticed: *"(Mock phase-1 repos carry none, per obs-017.)"* — an assumption true
of the derma mock and false of every repo in this fleet.

## What to change

Bring the resume path's treatment of ignored files into line with its sibling's ratified
ruling: preserve them rather than delete-and-refuse. The refusal exists only to protect against
a deletion the restore should not be performing.

Think carefully about what "preserve" means for the restore's correctness claim, and write down
what you conclude:

* The snapshot materialised in step (3) is built from tracked/non-ignored content, so ignored
  paths are not part of what a restore reestablishes either way.
* Therefore a restore that leaves ignored files alone is not LESS exact about the thing it
  claims to restore — it is exact about the same thing, and silent about paths that were always
  out of scope.
* The restore's own verification step compares what it materialised. Make sure whatever you
  change does not weaken that comparison, and say explicitly in the code what the restore does
  and does not now guarantee about ignored paths.

## Hard constraints

1. **Do not weaken the refusals that protect TRACKED work.** The clean-tree precondition, the
   base-commit check, the submodule refusal and both restore-digest verifications stay exactly
   as they are. Only the ignored-file treatment changes.
2. **No data loss.** The change must make it impossible for a resume to delete a `.venv`, a
   `.env` or any other ignored path. If your approach could still delete one on some path, it is
   the wrong approach.
3. **Do not edit `real_restore_tree`.** It is already correct; it is the precedent being
   followed, not the thing being changed.
4. The existing checkpoint and resume suites stay green. Edit an existing test ONLY where it
   pins the old delete-and-refuse behaviour directly, and say in the commit which and why.
5. `.claude/verify.sh` stays green.

## The durable half

A resume that works once on this machine is not the deliverable. Add a test that would have
caught this: a checkpointed run resumed in a repo that CARRIES an ignored file, asserting the
resume proceeds AND the ignored file still exists afterwards with its contents intact.

That test is the whole point — the defect survived review because every test repo was pristine,
exactly as the comment beside the refusal admitted.
