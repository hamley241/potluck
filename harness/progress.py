"""`progress.json`: the station's early signal (ruling 002 R2).

A run with a run directory publishes its lifecycle here so a supervisor can see
the phase it is in, the call it is waiting on and the deadline each declares.
The file is rewritten ATOMICALLY (temp file in the same directory, then rename)
on every state change and, from a dedicated thread, every `heartbeat_seconds`.
The heartbeat runs on its own thread because the loop thread blocks in
synchronous work (the checkpoint's `git add -A`, startup, finalization) and a
loop-task heartbeat would stall with it. A fresh `written_at` therefore proves
the PROCESS is responsive, and nothing more; the phase deadline is what bounds
a hang inside a phase.

Never read on resume: it is a signal for the supervisor, not run state.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

CANON_VERSION = "potluck-progress-v1"
SCHEMA_VERSION = 1
FILENAME = "progress.json"
HEARTBEAT_SECONDS = 15

# Declared bounds for the phases that are not a bounded call (R2). `in_call`
# uses the call's own timeout instead.
STARTING_DEADLINE_SECONDS = 300
BETWEEN_CALLS_DEADLINE_SECONDS = 300
FINALIZING_DEADLINE_SECONDS = 600

_CALL_FIELDS = ("step", "call_index", "call_started_at", "call_deadline_seconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressWriter:
    """Owns `<run_dir>/progress.json` for one run. Every mutation and every
    write happens under one lock, so the heartbeat thread and the loop thread
    never interleave a half-updated state and `seq` strictly increases."""

    def __init__(self, run_dir, *, run_manifest_id: str, limits: dict,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS):
        self.path = Path(run_dir) / FILENAME
        self.heartbeat_seconds = heartbeat_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._terminal = False
        self._state: dict = {
            "run_manifest_id": run_manifest_id,
            "pid": os.getpid(),
            "limits": limits,
            "worktree": None,
            "implement_prompt_bytes": None,
            "attempts": {},
            "last_completed": None,
        }
        self._enter("starting", STARTING_DEADLINE_SECONDS)

    @property
    def phase(self) -> str:
        return self._state["phase"]

    # --- lifecycle ---

    def start(self) -> None:
        """Write the first record and start the heartbeat thread."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write()
        self._thread = threading.Thread(target=self._beat, name="potluck-progress",
                                        daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            self.write()

    def finish(self, phase: str, fields=None, **extra) -> None:
        """Write the terminal record (`done` or `aborted`) and stop the heartbeat.
        Terminal phases carry no deadline; nothing is written after this.

        `fields`, when given, returns more terminal fields. It is asked AFTER the
        heartbeat is joined and asked again once the record is on disk; if the
        answer changed (a signal landed during the write) the record is rewritten,
        so it reflects every change made before it was committed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            for k in (*_CALL_FIELDS, "phase_started_at", "phase_deadline_seconds"):
                self._state.pop(k, None)
            self._state["phase"] = phase
            self._state.update(extra)
            while True:
                now = fields() if fields is not None else {}
                self._state.update(now)
                self._write_locked()
                if fields is None or fields() == now:
                    break
            self._terminal = True

    # --- state changes (each one is written immediately) ---

    def _enter(self, phase: str, deadline_seconds: int) -> None:
        self._state["phase"] = phase
        self._state["phase_started_at"] = _now()
        self._state["phase_deadline_seconds"] = deadline_seconds

    def _update(self, fn) -> None:
        with self._lock:
            if self._terminal:
                return
            fn(self._state)
            self._write_locked()

    def set(self, **fields) -> None:
        self._update(lambda s: s.update(fields))

    def attempt(self, step: str) -> None:
        def f(s):
            s["attempts"][step] = s["attempts"].get(step, 0) + 1
        self._update(f)

    def call_started(self, step: str, call_index: int, deadline_seconds: int) -> None:
        def f(s):
            self._enter("in_call", deadline_seconds)
            s.update(step=step, call_index=call_index,
                     call_started_at=s["phase_started_at"],
                     call_deadline_seconds=deadline_seconds)
        self._update(f)

    def call_ended(self, completed_step: str | None = None) -> None:
        """Leave `in_call`; the call fields are removed. `completed_step` is the
        step whose call returned a result, recorded as `last_completed`."""
        def f(s):
            for k in _CALL_FIELDS:
                s.pop(k, None)
            self._enter("between_calls", BETWEEN_CALLS_DEADLINE_SECONDS)
            if completed_step is not None:
                s["last_completed"] = {"step": completed_step,
                                       "at": s["phase_started_at"]}
        self._update(f)

    def finalizing(self) -> None:
        def f(s):
            for k in _CALL_FIELDS:
                s.pop(k, None)
            self._enter("finalizing", FINALIZING_DEADLINE_SECONDS)
        self._update(f)

    # --- the atomic write ---

    def write(self) -> None:
        with self._lock:
            if not self._terminal:
                self._write_locked()

    def _write_locked(self) -> None:
        self._seq += 1
        doc = {"canon_version": CANON_VERSION, "schema_version": SCHEMA_VERSION,
               **self._state, "seq": self._seq, "written_at": _now(),
               "heartbeat_seconds": self.heartbeat_seconds}
        data = json.dumps(doc, indent=2, sort_keys=True).encode()
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=f".{FILENAME}.", suffix=".tmp",
                                       dir=str(self.path.parent))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            # A failed write is visible as a stale written_at; it never alters
            # the run it reports on.
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
