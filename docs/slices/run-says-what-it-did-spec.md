# A run that fails should say what it did and where it reached

Two declared follow-ups, one change: when a run ends badly, its artifacts should let someone
diagnose it without re-running anything. Today they do not, in two specific ways.

## Half 1 — a no-change escalation records nothing to diagnose from

When the doer produces no diff, the run terminates `escalated_no_signal` with *"doer:implement
produced no changes; a fix run that changed nothing has not done the job"*. The refusal is
right. The problem is that nothing in the log, the checkpoint or the station-run artifact
records what the doer SAID, so the cause cannot be recovered from the artifacts.

This has fired repeatedly, and one instance is instructive: the spec was almost certainly
UNSATISFIABLE — it demanded a config value that an existing test pinned, while its acceptance
forbade editing existing tests. If so the doer was right to refuse, and the harness's failure is
that it could only say "produced no changes" rather than surfacing the doer's own account.

Capture the doer's reply on the no-change path and put it where a diagnosing reader will find
it. The reply is the only evidence of WHY nothing happened and it is currently discarded.

## Half 2 — the restore's scope is silent about everything it does not cover

`restore_tree(repo, tree)` is bound to one repo, and the new run worktree confines the TARGET
tree only. A doer that edits a different repo under an authorizing spec leaves that edit in
place: no restore, no receipt, no record it happened. This is not hypothetical — a sibling
session's timed-out run left a live edit in `menu` that turned menu's gate red, and it was found
by the gate failing rather than by anything the run said. A run that SUCCEEDED would have left
it just as silently.

Nothing here can detect an edit in a repo the run never knew about, and the change must not
pretend to. What it CAN do is stop implying coverage it does not have: state the restore's scope
explicitly, so "nothing was restored elsewhere" is a recorded fact rather than an assumption a
reader has to make.

## What NOT to attempt

Three things are deliberately out of scope, named so they are not quietly attempted:

* **Detecting foreign-repo edits.** The run cannot enumerate repos it was never told about.
  Disclosure only.
* **Confining the doer.** Keeping the doer inside the target tree is a separate, larger slice.
* **Exact doer token usage.** Text-mode `claude -p` returns no usage envelope; that is the §2
  follow-up and stays deferred.

## Hard constraints

1. **Capture must not change what the run DECIDES.** A no-change escalation stays a no-change
   escalation; this adds evidence, never a different outcome.
2. **Do not log secrets.** The doer's reply is model output about the operator's code; it can
   quote files. Think about where it is written and who can read it, and say what you decided.
3. Nothing here may weaken the worktree isolation or the restore that landed in `96edc60`.
4. `.claude/verify.sh` stays green.

## The durable half

A test that a no-change run leaves the doer's reply recoverable from its artifacts — not merely
that a field exists, but that the text the doer produced can be read back after the run ends.

## On the acceptance below

It is deliberately narrow. Three times today I wrote acceptance criteria that could not be met —
one self-contradictory, one unachievable by any implementation, one inviting a mechanism that
could not be built safely. If a criterion here cannot be satisfied, say so and propose the
amended wording rather than contorting the code to fit it.
