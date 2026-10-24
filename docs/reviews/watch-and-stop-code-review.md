# Watch and stop — codex code review

Reviewer codex, `gpt-6.1-sol`. Declared 2 rounds, spent 2.

## Round 1 (over the station's commit) — CHANGES: 1 MAJOR, 2 MINOR

- **MAJOR — Late signals can still disappear from the terminal record.** [harness/progress.py:106](/Users/gp/development/autos/potluck/harness/progress.py:106), [harness/cli.py:930](/Users/gp/development/autos/potluck/harness/cli.py:930). A first SIGTERM arriving after the final `fields()` comparison—or after `finish()` returns but before handler restoration—sets `_LIFE.late`, but nothing rewrites `progress.json`. The run exits normally with no `late_signal`, violating A9. The new tests inject signals before this remaining window.

- **MINOR — Resume resets the sequence counter.** [harness/progress.py:56](/Users/gp/development/autos/potluck/harness/progress.py:56). Every new writer starts at zero, including `--resume` against an existing run directory. Its first write replaces the previous record with `seq: 1`, violating A3’s strictly increasing sequence across writes. No test covers sequence continuity on resume.

- **MINOR — The failed-rename test cannot detect destruction of the previous record.** [harness/test_progress.py:154](/Users/gp/development/autos/potluck/harness/test_progress.py:154). The assertion examines `before`, captured before the failure, and calls `finish()` before inspecting directory contents. If the failed update truncates or deletes `progress.json`, a successful terminal write repairs it and the test stays green. Read and compare the on-disk bytes immediately after the failed update, before `finish()`.

Acceptance audit: A2, A4–A8, A10 and A13 are supported by the implementation and targeted tests. A3 and A9 have the gaps above. A11 has one explicit abort producer, but its regex test does not prove indirect exit paths cannot return 3. A12’s signal-handler replacement prevents `asyncio.Runner` from installing its own SIGINT handler; that affects signalled runs, so it alone is not a no-signal regression. A14 is incomplete: several new checks lack the required failure comment. A1 and A15 were not independently verified; the read-only environment prevents running the suite’s filesystem mutations.

Cancellation reaches the gate, reviewer and tiebreaker through the shared subprocess helper, subject to the ruling’s explicitly deferred process-group containment gap. The heartbeat is daemonized and joined before terminal writing, preventing subsequent heartbeat writes.

VERDICT: CHANGES

## Round 2 (over `819d5690926e92ca9a4c5075e282a1d28aecb997`) — SHIP, 1 NIT (filed: FU-progress-docstring-resume-read)

- **NIT — harness/progress.py:65:** Resume now reads `progress.json` to seed `seq`, contradicting the module’s “Never read on resume” statement at line 13 and the slice spec. A maintainer following those statements could remove the read and reintroduce seq resets. Clarify that only seq is recovered; execution state is not.

The four folds otherwise look correct. In-memory checks passed for seq seeding, late amendment, terminal phase preservation, and idempotence. Full suite not rerun because the sandbox is read-only.

VERDICT: SHIP
