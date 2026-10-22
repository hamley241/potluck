# A failed run says what it did — codex findings

Review budget capped at THREE rounds before the review began, after the previous slice ran to
ten. No BLOCKING at any round.

## Round 1 — 4 MAJOR, 2 MINOR

- `restore-scope-wrong-path` — the scope named the operator's checkout as covered. **Its test
  pinned the defect.** Both corrected.
- `RESTORE-SCOPE-ALLOW-DIRTY` — `--allow-dirty` sets `restore_tree = None`, so nothing is
  restored, and the statement still claimed worktree coverage.
- `no-change-recovery-optional` (unanswered_fork) — recovery depended on the optional
  `--log-file`. Now also written to the `--run-dir` sidecar, with an explicit note when neither.
- `unbounded-reply-can-alter-termination` (unanswered_fork) + `NOCHANGE-REPLY-UNBOUNDED-WRITE` —
  an uncapped write can crash the declared exit. Capped at 256 KiB, every persist wrapped.
- `NOCHANGE-STDOUT-CONFINEMENT` — the comment overstated confinement; a reply shorter than the
  cap is complete in the stdout preview too.

## Round 2 — 2 MAJOR

- `stale-no-change-reply-on-orchestrator-reuse` — the field lived on `self` and was never reset,
  so a reused Orchestrator could attach a prior run's reply to an unrelated escalation.
  `run_feature` already boxes per-feature state in `_RunState()` for exactly this reason; this
  field was the one breaking the pattern beside it.
- `capture-warning-can-alter-outcome` — the handlers preventing a crash each ended in a bare
  stderr `print`, which can raise on a broken pipe.

## Round 3 — 2 MAJOR, resolved by amending A1

- `no-change-artifact-not-guaranteed` and `reply-persistence-truncates-actual-output` both
  indict A1's absolutes rather than the code. A1 amended in writing; the truncation boundary,
  an evidence gap rather than a conflict, was closed with a test.

## The shape

Zero BLOCKING, three rounds, ~290k tokens — against six BLOCKING, ten rounds and ~1.1M for the
previous slice. The difference was a spec with an explicit out-of-scope section and an
invitation to reject unachievable criteria.
