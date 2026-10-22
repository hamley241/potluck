# potluck — declared follow-ups (named, scoped, deferred)

## FU-resume-ignored-files — `--resume` is unusable in every fleet repo

**The gap.** The two restore paths made OPPOSITE rulings on `git clean -x`, and only one wrote
down why. `real_restore_tree` (end of a failed run, `orchestrator.py`) uses `clean -fd` and
PRESERVES ignored files, with an explicit rationale: *"-x is deliberately NOT used here: it would
delete .venv, node_modules, and .env — destroying local environments and secrets, a far worse
data-loss footgun than the hole it closes."* `restore_tree` (the resume path, `checkpoint.py`)
uses `clean -fdx`, so it must refuse up front rather than destroy ignored work — on the stated
assumption *"(Mock phase-1 repos carry none, per obs-017.)"*

**Why it bites now.** That assumption is false for the fleet's own repos: `menu` carries `.venv/`
plus five `runs/` directories, `potluck` and `harness_core` carry `.venv/`, `conductor` carries
`__pycache__/`. So resume refuses everywhere with *"ignored files are present that `clean -fdx`
would delete"*. The resumability work the first-customer report called **"the headline fix"**
cannot run on the fleet that owns it. Observed live 2026-10-22.

**Design direction.** The sibling path's ruling is the precedent: preserve ignored files rather
than delete-and-refuse. Needs care — the snapshot's own ignored blobs and post-snapshot ignored
edits are different cases, and the current refusal exists to protect the second.

## FU-crossrepo-restore-escape — a foreign-repo edit survives a failed run unrecorded

**The gap.** `restore_tree(repo, tree)` is bound to ONE repo path. If the doer edits a DIFFERENT
repo under an authorizing spec, a failed run restores only the target; the foreign edit survives
with no restore, no receipt and no record that it happened.

**Evidence.** A sibling session's timed-out run in one repo left a live edit in `menu` that
turned menu's gate red. Nothing recorded it; it was found by the gate failing, and a run that
SUCCEEDED would have left it just as silently.

**Design direction.** Disclosure first (obs-017 blast radius): name the restore's scope in the
run record and on the failure path, so "nothing was restored elsewhere" is explicit rather than
assumed. Doer confinement to the target tree is the larger follow-up.

## FU-doer-noop-undiagnosable — an empty-diff escalation records nothing to diagnose from

**The gap.** Two `potluck fix` runs dispatched 2 doer calls each and produced no diff,
terminating `escalated_no_signal`. The refusal is right — *"a fix run that changed nothing has
not done the job… the loop cannot infer it"*. But nothing in the log, checkpoint or station-run
artifact records what the doer SAID, so the cause is undiagnosable from the artifacts.

**Suspected cause in this instance**, unconfirmed for exactly that reason: the spec was
unsatisfiable — it required a config value that an existing test pinned, while its acceptance
forbade editing existing tests. If so the doer was right to refuse, and the harness's failure is
that it could not say "your acceptance criteria are mutually unsatisfiable" instead of "produced
no changes".

**Design direction.** Capture the doer's reply on the no-change path at minimum. Related to the
§2 doer-usage gap: text-mode `claude -p` returns neither usage nor a capturable envelope.
