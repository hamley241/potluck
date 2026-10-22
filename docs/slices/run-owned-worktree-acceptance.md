A1. The doer and the gate run in a worktree created for the run, detached at the run's base.

A2. The operator's branch ref is NEVER moved by potluck — not on absorb, not on restore, not on
    success, not on failure. Demonstrated, not asserted.

A3. A foreign commit made on the operator's branch DURING a run survives the run's abandonment:
    still reachable, file still present, and the operator's branch ref, HEAD and `git status`
    unchanged from what the foreign session left. This is the reproduced destruction case and it
    must now be impossible.

A4. No commit the run did not create in its own worktree is ever destroyed.

A5. **AMENDED 2026-10-22 (codex BLOCKING r6, `run-worktree-a5-scope-narrowing`).** As first
    written — "no orphaned directory and no stale entry, on success AND on failure" — A5 is
    UNACHIEVABLE by any implementation: a SIGKILL bypasses every `finally`. Three folds were
    spent narrowing a window that cannot reach zero width, and a sweep written to close it
    raced a concurrent run. Declaring a gap against an unmet criterion is not the same as
    changing the criterion, so the criterion is changed, here, visibly.

    A5 (amended): the worktree is removed when the run ends on every path the PROCESS CAN
    OBSERVE — pass, escalation, refusal (SystemExit), exception, Ctrl-C. When removal does not
    fully succeed, the run REPORTS it, naming whether the directory, the `.git/worktrees` entry,
    or both survived, and stating that nothing removes them automatically. A hard kill is a
    declared gap with its blast radius recorded in the code: it costs disk and tidiness, never
    correctness — refs, tree and commits are untouched.

A6. Where the run's output goes on success is DECIDED and written down in the code, with its
    reasoning — not left implicit.

A7. The sibling-path trap is handled deliberately: a worktree whose `../harness_core` does not
    resolve either works anyway, or refuses with a message naming that cause. It never surfaces
    as an unexplained exit-2 gate.

A8. Ignored files are preserved on every restore path; no `-x` anywhere.

A9. Any precondition or refusal that changed is named in the commit with the reason it changed.

A10. New tests cover A2, A3 and A5, and fail if the worktree isolation is reverted.

A11. `./.claude/verify.sh` exits 0.
