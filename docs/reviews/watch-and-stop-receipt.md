# Watch and stop — receipt

Authorizes ruling 002 R1 and R2 (conductor `docs/decisions/002-early-signal.md`): the station's
`progress.json` from a heartbeat thread, and SIGTERM/SIGINT stopping a run before its commit
boundary with exit 3 and nothing published.

```reviewgate
reviewer.model: gpt-6.1-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 819d5690926e92ca9a4c5075e282a1d28aecb997
verdict: SHIP
date: 2026-10-23
findings: docs/reviews/watch-and-stop-code-review.md
```

**Built by potluck** (run `watch-and-stop`: doer claude, reviewer codex, **1 fix round** — potluck's
own review caught a commit-boundary race), spec 67 lines / acceptance 15 criteria committed first at
`72b0a55`. **The first run under the new lease:** `doer:implement` took **762s in one attempt**
with 13 edits — under the old 600s ceiling it would have been killed and restarted from nothing.

**Hand folds after codex r1** (one commit): the late signal lost after `finish()` (MAJOR — the
handler cannot take the writer's lock, so `cmd_fix` restores the handlers and then amends the
terminal record), `seq` continuity across a `--resume` writer, and the failed-rename test reading
the on-disk bytes. **Each new check was verified red by mutation** — no seq seeding, a no-op amend,
and amending before handler restoration each fail their check.

**A13, narrowed:** `progress.json` is read on resume for its `seq` only; no run state. Docstring
lag filed as `FU-progress-docstring-resume-read` rather than folded, as no round remained.

**Found during the run: sleep pauses the station's clock.** A lid-close stopped asyncio's monotonic
timers for ~12 minutes inside `apply_fixes`; it looked like a dead timer and was not. The station
behaved correctly. The consequence is for the conductor (C1): it must time on its own monotonic
clock, never wall clock against a station deadline.

**Gate.** `./test` exit 0, `ALL SUITES PASS`, **977** `[PASS]` (910 on `main`), no `[FAIL]`,
under `UV_PYTHON=3.11`. No stub process survived the suite. Siblings unmoved and clean; no residue.

**Budget:** declared 2 rounds, spent 2.
