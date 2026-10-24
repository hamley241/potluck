# Slice: a station that can be watched and stopped

**Authority: `conductor/docs/decisions/002-early-signal.md`, rulings R1 and R2** (sibling checkout;
read it, do not edit it). Where this spec and the ruling disagree, the ruling wins and the
disagreement is a finding. Reasoning lives there; this file is instruction only.

## What this slice builds

### 1. `progress.json`, written by a dedicated thread (R2)

A new module `harness/progress.py`. For every run with a run directory (`--run-dir` or `--resume`),
`<run_dir>/progress.json` is written **atomically** (temp file in the same directory, then rename),
`canon_version: "potluck-progress-v1"`, `schema_version: 1`. A bare `fix` with no run directory
writes nothing.

* **A heartbeat thread** rewrites it every `heartbeat_seconds` (15) from the moment the run directory
  is known until the terminal record is written. It must keep writing while the event loop's thread
  is blocked in synchronous work — that is the point of a thread.
* **Fields:** `run_manifest_id`, `pid`, `seq` (strictly increasing across every write), `written_at`
  (UTC ISO-8601), `heartbeat_seconds`, `phase`, `phase_started_at`, `phase_deadline_seconds`;
  when `phase == "in_call"` also `step`, `call_index` (the admission count), `call_started_at`,
  `call_deadline_seconds`; always `attempts` (a map of step name → times its call factory was
  invoked), `last_completed` (`{step, at}` or null), `limits` (the resolved timeouts, `c_max`,
  `n_max`, `feature_seconds` — the values `_effective_config_dict` binds), `worktree` (the run
  worktree's path), `implement_prompt_bytes` (null until known).
* **Phases:** `starting` → (`in_call` ↔ `between_calls`)* → `finalizing` → `done`, or `aborted`.
  Call fields are **removed** when a call ends. Each non-terminal phase has a declared deadline:
  `in_call` uses the call's own timeout; `starting`, `between_calls` and `finalizing` use named
  constants defined once in `progress.py` (300, 300 and 600 seconds).
* Call start and end are reported from `BudgetedStepRunner.run` (admission → `in_call`; return or
  raise → `between_calls`). `finalizing` begins when the feature's `asyncio.run` returns.

### 2. SIGTERM stops the run (R1)

`potluck fix` installs SIGTERM and SIGINT handlers that set a **process-wide abort latch** and, if the
feature task is running, cancel it from the loop's thread. The **commit boundary is entry to
`finalizing`**:

* **Latched before the boundary:** finalization never starts — no `station-run.json`, no export
  publication, no delivery. The existing cancellation teardown stops the call's child process group;
  `cmd_fix`'s `finally` removes the worktree as it already does; the terminal record is written with
  `phase: "aborted"`, `signal` (its name) and `interrupted_phase`; the process exits with status
  **3**, which no other path uses.
* **Received after the boundary:** finalization is not interrupted. The signal is recorded in
  `late_signal` on the terminal `done` record, and the run exits with its normal status.
* A repeated signal sets nothing new and must not interrupt the teardown already in progress.

## What must not change

* A run with no signal behaves exactly as today: same outputs, same exit statuses, same artifacts.
* `station-run.json` stays v1. The checkpoint format is unchanged; `progress.json` is not read on
  resume. `harness_core`, `menu`, `conductor` untouched — one repository.

## Tests

Unit tests for the writer (atomicity, `seq`, phase transitions, field removal, deadlines) and **at
least one real-process test**: run `potluck fix` as a subprocess with a stub `claude` placed first on
`PATH` (a script that sleeps), send SIGTERM while `in_call`, and assert exit 3, an `aborted` record,
no `station-run.json`, no export or delivery, no surviving stub process, and no remaining
`.<repo>.potluck-run-*` worktree. Also: a signal during finalization yields `late_signal` with
publication intact; the heartbeat advances while the loop thread is blocked in a synchronous call.

**For every new test, state in a comment what would have to break for it to fail.**

## Review budget

**2 rounds, declared.** An overrun is declared in the receipt.
