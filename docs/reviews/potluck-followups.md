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

## FU-doer-commit-defeats-detection — a doer that COMMITS defeats both the no-change check and the rollback

**The gap, observed live 2026-10-22.** A `potluck fix` run reported
`escalated_no_signal` / *"doer:implement produced no changes"* and ran **no gate** — while the
doer had in fact written a 5-file, +222/-71 change and **committed it**. potluck's no-change
detection compares the WORKING TREE against the baseline; a doer that commits moves its work
into HEAD, so the tree is clean and the run concludes nothing happened.

**The dangerous half is the rollback.** `real_restore_tree` runs `git reset --hard` with NO ref,
resetting to HEAD. If the doer moved HEAD, the "restore to the pre-implement baseline" resets to
the DOER'S COMMIT — so the restore cements the work instead of discarding it. Confirmed: the
commit was still on the branch after the escalation, and the branch HEAD had moved off its base.

**Why it matters more than a mis-report.** The run says "nothing happened" while a substantial
unreviewed, ungated change sits committed. A less careful operator takes the escalation at its
word and the change ships unnoticed on the next push, or is attributed to whoever commits next.
It also means an escalated run is NOT safely abandonable, which is the property the restore
exists to provide.

**Not the doer misbehaving.** Committing finished work is reasonable; potluck simply never
specified the contract. The three earlier runs in this fleet did NOT commit, so the behaviour is
not deterministic and cannot be relied on either way.

**Design direction.** Detect against the BASE COMMIT, not the working tree: compare
`git rev-parse HEAD` to the captured `repo_base_commit` as well as diffing the tree, so a moved
HEAD counts as a change. Restore to the captured base explicitly (`git reset --hard <base>`)
rather than to bare HEAD. State the doer contract either way.

**Trip condition: before any unattended run.** An unattended run that escalates and silently
leaves committed work is the worst shape this failure can take.

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

## FU-nofollow-temp-writes — a link installed after the preflight scan can still be followed

**Status: DECLARED by codex at round 2 of the import slice's review (SHIP), non-BLOCKING.** Recorded
rather than folded, because the review budget was 3 rounds and the finding is a concurrency
hardening, not a reachable defect in the slice's own paths.

**The gap.** `cli._refuse_links_into_import` walks every existing entry under each output destination
(`--run-dir`, `--export-dir`, `--log-file`) before anything is written and refuses, by name, a
symlink whose `realpath` is the import or inside it. codex confirmed that closes the **pre-existing**
case for every write this slice makes — the gate copy, the checkpoint writes, `persist_run_inputs`
and the station-run write are all immediate children of the resolved run directory, so the scan's
lack of traversal through nested directory symlinks leaves no bypass there. The import marker has its
own no-follow write on top.

**What remains: a link installed AFTER the scan**, by a concurrent writer, can still be followed by
those ordinary temporary-file writes. The scan is a point-in-time check; the writes are not
self-protecting.

**Remedy, as ruled:** use no-follow temporary-file creation **consistently** — the
`unlink` + `O_CREAT|O_EXCL|O_NOFOLLOW` discipline that `export._write_atomic` and
`cli._write_nofollow` already use — for every write into a caller-supplied directory, rather than
relying on the preflight scan alone.

**Trip condition: before any unattended run that takes a caller-supplied run-dir from an untrusted
or shared location**, and in any case when part 3 adds delivery's own writes. Two instances of this
exact class have now been found by review rather than by a gate (part 1's `_write_atomic`, part 2's
import marker), so the next write into a caller-supplied path should start no-follow rather than
be retrofitted.
