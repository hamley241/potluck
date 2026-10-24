# Acceptance: a station that can be watched and stopped

Graded against `watch-and-stop-spec.md` and ruling 002 R1/R2. If a criterion is wrong or
unreachable, amend it here in writing and say why.

**A1.** `./test` exits 0 with `ALL SUITES PASS`, with at least the 910 checks `main` has today.

**A2.** A run with a run directory writes `progress.json` carrying every field the spec names, with
`canon_version "potluck-progress-v1"`; a bare `fix` with no run directory writes none.

**A3.** Every write is atomic (temp + rename in the same directory) and `seq` strictly increases.

**A4.** The heartbeat thread advances `written_at` and `seq` while the loop thread is blocked in a
synchronous call longer than two heartbeat intervals.

**A5.** Phases follow `starting → (in_call ↔ between_calls)* → finalizing → done|aborted`; call fields
exist only while `in_call`; each non-terminal phase carries `phase_started_at` and
`phase_deadline_seconds`, the latter equal to the call's timeout when `in_call`.

**A6.** `attempts` counts factory invocations per step; `limits` equals the run's resolved timeouts,
`c_max`, `n_max` and `feature_seconds`.

**A7.** Real-process test: SIGTERM while `in_call` exits **3**, writes `phase "aborted"` with `signal`
and `interrupted_phase`, writes no `station-run.json`, publishes no export, delivers nothing.

**A8.** In that test no stub process survives and no `.<repo>.potluck-run-*` worktree or
`.git/worktrees` entry remains.

**A9.** A signal received during `finalizing` is recorded as `late_signal`; publication completes and
the run exits with its normal status.

**A10.** A second signal during teardown does not interrupt it: the A7/A8 outcome still holds.

**A11.** Exit status 3 is used by no other path.

**A12.** A run that receives no signal is unchanged: same outputs, exit statuses and artifacts as
before this slice.

**A13.** `station-run.json` stays v1; the checkpoint format is unchanged; resume does not read
`progress.json`.

**A14.** Every new test carries a comment stating what would have to break for it to fail.

**A15.** One repository: no file outside this checkout is modified, verified against every sibling.
