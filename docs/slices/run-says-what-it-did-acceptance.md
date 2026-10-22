A1. **AMENDED 2026-10-22 (codex MAJOR r3, `no-change-artifact-not-guaranteed` +
    `reply-persistence-truncates-actual-output`).** As written — recoverable verbatim from "the
    run's artifacts" — it cannot hold in two cases the wording ignored: a run given NEITHER
    `--log-file` nor `--run-dir` has no durable artifact to recover from, and a model reply is
    unbounded while a persisted write must be capped so it cannot fail and change the run's
    declared exit (A3). Contorting the code to satisfy the sentence would mean either forcing an
    artifact the operator did not ask for, or an unbounded write that can crash the failure path.

    A1 (amended): when a durable artifact was requested (`--log-file` or `--run-dir`), the
    doer's reply is recoverable from it VERBATIM up to 256 KiB, with truncation and the true
    length recorded so a reader knows what they have. When NEITHER was requested, the run states
    on stderr that the reply was not persisted and how to capture it next time — disclosure, not
    recovery, and labelled as such.

A2. The captured reply is the doer's actual output for that step. An empty or missing reply is
    recorded as empty/missing and distinguishable from "not captured".

A3. Capture changes no outcome: the same run still terminates `escalated_no_signal` with the
    same reason. Adding evidence must not move a verdict.

A4. The restore's SCOPE is stated explicitly where a diagnosing reader meets it — naming what it
    covers and, in the same breath, that edits outside it are neither restored nor detected.

A5. Nothing claims to detect foreign-repo edits, and nothing attempts to.

A6. Where the doer's reply is written, and who can read it, is a DECIDED question answered in
    the code with its reasoning — the reply can quote the operator's source.

A7. The worktree isolation and restore from `96edc60` are unchanged in behaviour.

A8. A new test reads a captured reply back from a no-change run's artifacts and fails if capture
    is removed.

A9. `./.claude/verify.sh` exits 0.
