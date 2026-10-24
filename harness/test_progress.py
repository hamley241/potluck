"""A station that can be watched and stopped (ruling 002 R1/R2).

No model calls. Covers:
  writer    -- progress.json is written atomically (temp + rename in the same
               directory), `seq` strictly increases, call fields exist only while
               `in_call`, every non-terminal phase declares its deadline, and
               nothing is written after the terminal record.
  heartbeat -- the heartbeat thread keeps writing while the loop thread is blocked
               in a synchronous call.
  lifecycle -- a real `cmd_fix` with --run-dir walks starting -> in_call <->
               between_calls -> finalizing -> done with every named field; a bare
               `fix` writes no progress.json.
  late      -- a SIGTERM during finalization is recorded as late_signal and the
               export is still published.
  abort     -- a REAL `potluck fix` subprocess with a sleeping stub `claude` first
               on PATH, SIGTERMed while in_call: exit 3, an aborted record, no
               station-run.json, no export, no delivery, no surviving stub, no
               worktree -- including when a second signal lands mid-teardown.
  gate/checkpoint -- SIGTERM during the gate and inside a synchronous checkpoint
               write also aborts with exit 3 and publishes nothing. Ruling 002
               requires both; the slice spec's list omits them, and the ruling
               wins.
  every path -- a signal latched before a startup refusal or during its cleanup
               still aborts; an unsignalled refusal is unchanged; a first signal
               while the terminal record is being written is in it.

Runnable: `python3 -m harness.test_progress`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import harness.cli as cli
from harness import checkpoint as ck
from harness import progress as pg
from harness import station_run as sr
from harness.export import MANIFEST_FILENAME
from harness.test_run_worktree import _Doer, _git, _no_worktree_left, _run_fix, _setup

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


# --- writer --------------------------------------------------------------------

def test_writer_atomic_seq_phases_deadlines():
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / "run"
        w = pg.ProgressWriter(rd, run_manifest_id="m", limits={"n_max": 9},
                              heartbeat_seconds=3600)
        replaced: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst)
        os.replace = spy
        try:
            w.start()
            first = _read(rd / pg.FILENAME)
            w.call_started("doer:implement", 1, 1200)
            in_call = _read(rd / pg.FILENAME)
            w.attempt("doer:implement")
            w.call_ended("doer:implement")
            between = _read(rd / pg.FILENAME)
            w.finalizing()
            fin = _read(rd / pg.FILENAME)
        finally:
            os.replace = real_replace
        # Fails if a write goes straight to progress.json (no temp file), or the
        # temp file lives in another directory (rename would not be atomic).
        _check("writer: every write is temp + rename in the same directory",
               len(replaced) == 5 and all(
                   Path(s).parent == Path(d).parent == rd and s != d
                   and d == str(rd / pg.FILENAME) for s, d in replaced), f"{replaced}")
        # Fails if the canon/schema stamps or the always-present fields are missing.
        _check("writer: canon_version, schema_version and the constant fields",
               first["canon_version"] == "potluck-progress-v1"
               and first["schema_version"] == 1 and first["run_manifest_id"] == "m"
               and first["pid"] == os.getpid() and first["limits"] == {"n_max": 9}
               and first["attempts"] == {} and first["last_completed"] is None
               and first["implement_prompt_bytes"] is None and first["worktree"] is None
               and first["heartbeat_seconds"] == 3600 and "written_at" in first, f"{first}")
        seqs = [d["seq"] for d in (first, in_call, between, fin)]
        # Fails if seq does not strictly increase across writes.
        _check("writer: seq strictly increases", seqs == sorted(set(seqs)), f"{seqs}")
        # Fails if a phase lacks its declared deadline or uses the wrong constant.
        _check("writer: starting/between_calls/finalizing deadlines are 300/300/600",
               (first["phase"], first["phase_deadline_seconds"]) == ("starting", 300)
               and (between["phase"], between["phase_deadline_seconds"])
               == ("between_calls", 300)
               and (fin["phase"], fin["phase_deadline_seconds"]) == ("finalizing", 600)
               and all("phase_started_at" in d for d in (first, between, fin)))
        # Fails if in_call's deadline is not the call's own timeout, or a call
        # field is missing while in_call.
        _check("writer: in_call carries the call fields and the call's timeout",
               in_call["phase"] == "in_call" and in_call["step"] == "doer:implement"
               and in_call["call_index"] == 1 and in_call["call_deadline_seconds"] == 1200
               and in_call["phase_deadline_seconds"] == 1200
               and in_call["call_started_at"] == in_call["phase_started_at"], f"{in_call}")
        # Fails if call fields survive the end of the call (an old deadline would
        # then apply to the next phase), or attempts/last_completed are not kept.
        _check("writer: call fields are removed when the call ends",
               not any(k in d for d in (between, fin) for k in pg._CALL_FIELDS)
               and between["attempts"] == {"doer:implement": 1}
               and between["last_completed"]["step"] == "doer:implement", f"{between}")

        w.finish("done", late_signal="SIGTERM")
        done = _read(rd / pg.FILENAME)
        w.set(worktree="/late")
        w.write()
        # Fails if the terminal record keeps a deadline, misses late_signal, or a
        # write after it replaces the terminal record.
        _check("writer: terminal record has no deadline and nothing follows it",
               done["phase"] == "done" and done["late_signal"] == "SIGTERM"
               and "phase_deadline_seconds" not in done
               and _read(rd / pg.FILENAME) == done and not w._thread.is_alive(), f"{done}")
        # Fails if a temp file is left behind by a write.
        _check("writer: no temp file left behind",
               sorted(p.name for p in rd.iterdir()) == [pg.FILENAME])


def test_writer_failed_rename_keeps_last_record():
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        w = pg.ProgressWriter(rd, run_manifest_id="m", limits={}, heartbeat_seconds=3600)
        w.start()
        before = (rd / pg.FILENAME).read_bytes()
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("disk full")
        os.replace = boom
        try:
            w.call_started("gate", 2, 60)
        finally:
            os.replace = real_replace
        # Read the bytes ON DISK right after the failed update, before anything else
        # writes (codex review r1: inspecting `before` alone, after finish(), let a
        # later successful write repair a destroyed record and stay green).
        after = (rd / pg.FILENAME).read_bytes()
        names = sorted(p.name for p in rd.iterdir())
        w.finish("done")
        # Fails if a failed write truncates/partially writes progress.json, leaves
        # its temp file, or raises into the run it reports on.
        _check("writer: a failed rename leaves the previous record whole, no temp",
               after == before and json.loads(after)["phase"] == "starting"
               and names == [pg.FILENAME], f"{names}")


def test_seq_continues_across_writers():
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        w1 = pg.ProgressWriter(rd, run_manifest_id="m", limits={}, heartbeat_seconds=3600)
        w1.start()
        w1.set(worktree="/a")
        w1.finish("done")
        last = _read(rd / pg.FILENAME)["seq"]
        # A --resume constructs a second writer over the same run dir.
        w2 = pg.ProgressWriter(rd, run_manifest_id="m", limits={}, heartbeat_seconds=3600)
        w2.start()
        first = _read(rd / pg.FILENAME)["seq"]
        w2.finish("done")
        # Fails if a new writer restarts seq at 1 (codex review r1 MINOR), so a
        # supervisor would read a resumed run's first record as older than the last.
        _check("writer: seq strictly increases across writers on one run dir",
               last >= 3 and first == last + 1, f"last={last} first={first}")


def test_late_signal_after_finish_is_amended():
    from harness import cli
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        life = cli._Lifecycle()
        life.progress = pg.ProgressWriter(rd, run_manifest_id="m", limits={},
                                          heartbeat_seconds=3600)
        life.progress.start()
        life.settle()                      # past the commit boundary
        life.finish(False)                 # the done record, no signal yet
        life.on_signal(signal.SIGTERM, None)   # lands in the window after finish()
        missed = _read(rd / pg.FILENAME)
        # Control: fails if the window does not exist (the record already carries
        # the signal), which would make the assertion below vacuous.
        _check("late-window: the done record misses a signal landing after finish()",
               missed.get("phase") == "done" and "late_signal" not in missed, f"{missed}")
        life.amend_late()
        amended = _read(rd / pg.FILENAME)
        # Fails if amend_late does not record the late signal (codex review r1 MAJOR),
        # or rewrites anything but the terminal record.
        _check("late-window: amend_late records it on the terminal record",
               amended.get("phase") == "done" and amended.get("late_signal") == "SIGTERM"
               and amended["seq"] == missed["seq"] + 1, f"{amended}")
        life.amend_late()
        # Fails if amending is not idempotent (each call would add a write).
        _check("late-window: amending twice writes once",
               _read(rd / pg.FILENAME)["seq"] == amended["seq"])
    # Fails if cmd_fix does not amend after restoring the handlers: amending
    # before restoration would leave the same window open.
    import inspect
    src = inspect.getsource(cli.cmd_fix)
    _check("late-window: cmd_fix amends after restoring the handlers",
           0 < src.find("signal.signal(sig, handler)") < src.find("_LIFE.amend_late()"))


def test_heartbeat_while_loop_thread_blocked():
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        w = pg.ProgressWriter(rd, run_manifest_id="m", limits={}, heartbeat_seconds=0.05)
        w.start()
        seen = {}

        async def blocked():
            seen["before"] = _read(rd / pg.FILENAME)
            time.sleep(0.4)            # synchronous: the loop thread cannot run
            seen["after"] = _read(rd / pg.FILENAME)
        asyncio.run(blocked())
        w.finish("done")
        b, a = seen["before"], seen["after"]
        # Fails if the heartbeat runs on the loop (it would stall here), or does
        # not rewrite written_at/seq: 0.4s is 8 intervals, we require >= 2.
        _check("heartbeat: advances seq and written_at while the loop is blocked",
               a["seq"] - b["seq"] >= 2 and a["written_at"] != b["written_at"]
               and a["phase"] == b["phase"] == "starting", f"{b['seq']}->{a['seq']}")


# --- lifecycle through the real cmd_fix -----------------------------------------

def test_lifecycle_fields_and_phases():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        rd = tmp / "run"
        records: list[dict] = []
        real_write = pg.ProgressWriter._write_locked

        def recording(self):
            real_write(self)
            records.append(_read(self.path))
        pg.ProgressWriter._write_locked = recording
        seen = {}

        def work():
            seen["in_call"] = _read(rd / pg.FILENAME)
            Path("feature.txt").write_text("new\n")
            return "implemented"
        doer = _Doer(work)
        try:
            code, err = _run_fix(repo, doer, ["--run-dir", str(rd)])
        finally:
            pg.ProgressWriter._write_locked = real_write
        args = cli.build_parser().parse_args(
            ["fix", "--spec", "x", "--no-debate", "--no-tiebreaker"])
        cfg = cli.load_config(args)
        want_limits = {**cli._effective_config_dict(cfg), "c_max": cfg.timeouts.c_max(),
                       "n_max": cfg.n_max()}
        final = _read(rd / pg.FILENAME)
        ic = seen.get("in_call", {})
        # Fails if reporting progress changes a no-signal run's outcome (A12).
        _check("lifecycle: run passed unchanged", code == 0, f"code={code} {err[-300:]}")
        # Fails if implement's admission is not reported as in_call with its own
        # timeout, index and step.
        _check("lifecycle: implement is in_call with the call's own deadline",
               ic.get("phase") == "in_call" and ic.get("step") == "doer:implement"
               and ic.get("call_index") == 1
               and ic.get("call_deadline_seconds") == ic.get("phase_deadline_seconds")
               == cfg.timeouts.resolve_doer_timeout("doer:implement"), f"{ic}")
        # Fails if limits differ from what _effective_config_dict binds (+ c_max,
        # n_max), or the worktree is not the run's worktree.
        _check("lifecycle: limits are the run's resolved limits",
               final["limits"] == want_limits, f"{final['limits']}")
        _check("lifecycle: worktree is the run's worktree",
               doer.cwds and os.path.realpath(final["worktree"]) == doer.cwds[0],
               f"{final['worktree']} {doer.cwds}")
        # Fails if attempts do not count one factory invocation per admitted call.
        _check("lifecycle: attempts count factory invocations per step",
               final["attempts"].get("doer:implement") == 1
               and sum(final["attempts"].values())
               == max(r.get("call_index", 0) for r in records), f"{final['attempts']}")
        phases = [r["phase"] for r in records]
        collapsed = [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p]
        middle = collapsed[1:-2]
        # Fails if the phase order is broken anywhere (e.g. finalizing before the
        # last call ends, or done without finalizing).
        _check("lifecycle: starting -> (in_call <-> between_calls)* -> finalizing -> done",
               collapsed[0] == "starting" and collapsed[-2:] == ["finalizing", "done"]
               and middle and middle[0] == "in_call"
               and all(p == ("in_call" if i % 2 == 0 else "between_calls")
                       for i, p in enumerate(middle)), f"{collapsed}")
        # Fails if a call field exists outside in_call, or a non-terminal record
        # lacks phase_started_at/phase_deadline_seconds.
        _check("lifecycle: call fields only while in_call; every phase has a deadline",
               all((r["phase"] == "in_call") == all(k in r for k in pg._CALL_FIELDS)
                   and (r["phase"] == "in_call" or not any(k in r for k in pg._CALL_FIELDS))
                   for r in records)
               and all("phase_started_at" in r and "phase_deadline_seconds" in r
                       for r in records if r["phase"] != "done"))
        seqs = [r["seq"] for r in records]
        # Fails if two writers race (no shared lock) or a write reuses a seq.
        _check("lifecycle: seq strictly increases across the whole run",
               all(b > a for a, b in zip(seqs, seqs[1:])), f"{seqs}")
        # Fails if a no-signal run records a signal or misses its last call.
        _check("lifecycle: terminal done, no signal, last_completed set",
               final["phase"] == "done" and "late_signal" not in final
               and "signal" not in final and final["last_completed"] is not None)
        _check("lifecycle: station-run.json still v1 and written",
               (rd / sr.STATION_RUN_FILENAME).is_file())


def test_bare_fix_writes_no_progress():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)

        def work():
            Path("feature.txt").write_text("new\n")
            return "implemented"
        code, _ = _run_fix(repo, _Doer(work))
        # Fails if a run with no run directory writes progress.json anywhere.
        _check("bare: no run directory, no progress.json",
               code == 0 and not list(tmp.rglob(pg.FILENAME)))


def test_late_signal_keeps_publication():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        rd, out = tmp / "run", tmp / "export"
        real_write = sr.write_station_run_artifact

        def signalled(*a, **kw):
            os.kill(os.getpid(), signal.SIGTERM)   # lands inside finalizing
            time.sleep(0.05)
            return real_write(*a, **kw)
        sr.write_station_run_artifact = signalled

        def work():
            Path("feature.txt").write_text("new\n")
            return "implemented"
        try:
            code, err = _run_fix(repo, _Doer(work), [
                "--run-dir", str(rd), "--export-dir", str(out), "--export-task", "t"])
        finally:
            sr.write_station_run_artifact = real_write
        final = _read(rd / pg.FILENAME)
        # Fails if a signal after the commit boundary interrupts publication,
        # changes the exit status, or is not recorded as late_signal.
        _check("late: exit 0, station-run.json and export manifest published",
               code == 0 and (rd / sr.STATION_RUN_FILENAME).is_file()
               and (out / MANIFEST_FILENAME).is_file(), f"code={code} {err[-300:]}")
        _check("late: terminal done records late_signal",
               final["phase"] == "done" and final.get("late_signal") == "SIGTERM"
               and "interrupted_phase" not in final, f"{final}")
        ok, detail = _no_worktree_left(repo)
        # Fails if a late signal skips cmd_fix's worktree removal.
        _check("late: worktree removed", ok, detail)


# --- abort: a real potluck process ----------------------------------------------

_STUB = """#!/usr/bin/env bash
echo $$ >> "$POTLUCK_STUB_PIDS"
echo change > delivered.txt
trap '' TERM
sleep 300
"""


def _alive_in_group(pgid: int) -> list[str]:
    ps = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True)
    return [ln for ln in ps.stdout.splitlines()
            if ln.split() and ln.split()[1] == str(pgid)]


def _abort_run(export: bool, second_signal: bool):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        rd, out, stubs = tmp / "run", tmp / "export", tmp / "bin"
        stubs.mkdir()
        (stubs / "claude").write_text(_STUB)
        (stubs / "claude").chmod(0o755)
        pids = tmp / "stub_pids"
        env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}",
               "POTLUCK_STUB_PIDS": str(pids)}
        cmd = [sys.executable, "-P", "-m", "harness.cli", "fix", "--spec", "x",
               "--no-debate", "--no-tiebreaker", "--run-dir", str(rd)]
        if export:
            cmd += ["--export-dir", str(out), "--export-task", "t"]
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        rec = {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                rec = _read(rd / pg.FILENAME)
            except (OSError, ValueError):
                rec = {}
            if rec.get("phase") == "in_call" and pids.exists() and pids.read_text():
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        if second_signal:
            time.sleep(0.1)            # inside the stub's 0.5s SIGKILL grace
            proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        final = _read(rd / pg.FILENAME) if (rd / pg.FILENAME).exists() else {}
        stub_pids = [int(p) for p in pids.read_text().split()] if pids.exists() else []
        survivors = [ln for p in stub_pids for ln in _alive_in_group(p)]
        for p in stub_pids:            # a failing run must not leak its stub
            try:
                os.killpg(p, signal.SIGKILL)
            except OSError:
                pass
        wt_ok, wt_detail = _no_worktree_left(repo)
        return dict(code=proc.returncode, seen=rec, final=final, stderr=stderr,
                    station_run=(rd / sr.STATION_RUN_FILENAME).exists(),
                    exported=(out / MANIFEST_FILENAME).exists(),
                    status=_git(repo, "status", "--porcelain"),
                    delivered=(repo / "delivered.txt").exists(),
                    stub_pids=stub_pids, survivors=survivors,
                    wt_ok=wt_ok, wt_detail=wt_detail, pid=proc.pid)


def test_sigterm_in_call_aborts():
    for label, export, second in (("abort", False, False),
                                  ("abort+export+2nd-signal", True, True)):
        r = _abort_run(export, second)
        f = r["final"]
        # Precondition, and fails if the station never reports in_call for the real
        # implement -- every check below would then test a different abort point.
        _check(f"{label}: SIGTERM was sent while in_call on doer:implement",
               r["seen"].get("phase") == "in_call"
               and r["seen"].get("step") == "doer:implement" and r["stub_pids"],
               f"{r['seen']}")
        # Fails if the signal is not handled (exit -15), the abort is mistaken for
        # an ordinary failure (exit 1), or the second signal broke the teardown.
        _check(f"{label}: exit status 3", r["code"] == 3,
               f"code={r['code']} {r['stderr'][-600:]}")
        # Fails if no aborted terminal record is written, or it does not name the
        # signal and the phase it interrupted.
        _check(f"{label}: aborted record with signal and interrupted_phase",
               f.get("phase") == "aborted" and f.get("signal") == "SIGTERM"
               and f.get("interrupted_phase") == "in_call" and f.get("pid") == r["pid"]
               and not any(k in f for k in pg._CALL_FIELDS), f"{f}")
        # Fails if the real implement's prompt_size never reaches progress.json.
        _check(f"{label}: implement_prompt_bytes recorded from the real implement",
               isinstance(f.get("implement_prompt_bytes"), int)
               and f["implement_prompt_bytes"] > 0, f"{f.get('implement_prompt_bytes')}")
        # Fails if finalization ran despite the latch.
        _check(f"{label}: no station-run.json, no export, nothing delivered",
               not r["station_run"] and not r["exported"] and not r["delivered"]
               and r["status"] == "", f"{r['status']!r}")
        # Fails if the call's child group was not torn down on cancellation.
        _check(f"{label}: no stub process survives", not r["survivors"],
               f"{r['survivors']}")
        # Fails if cmd_fix's finally did not remove the worktree on abort.
        _check(f"{label}: no worktree or .git/worktrees entry remains", r["wt_ok"],
               r["wt_detail"])


# --- abort at the other points ruling 002 names (in process) ---------------------
#
# Ruling 002 requires an abort during the gate and during a synchronous checkpoint
# write to be tested alongside the implement cancellation above. The slice spec's
# test list names only the implement case; the ruling is authoritative, so both
# are covered here, against the real cmd_fix with a real gate and real
# checkpoint writes. The signal is sent to the MAIN thread with pthread_kill, so
# it is the loop thread that takes it, exactly as it is in a real process.

def _signal_main() -> None:
    signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)


_SLOW_GATE = """#!/usr/bin/env bash
echo $$ >> "$POTLUCK_GATE_PIDS"
trap '' TERM
sleep 300
"""


def _check_aborted(label: str, rd: Path, out: Path, repo: Path, code, err: str,
                   phase: str | None) -> dict:
    final = _read(rd / pg.FILENAME)
    # Fails if the abort is mistaken for an ordinary failure, or not handled.
    _check(f"{label}: exit status 3", code == 3, f"code={code} {err[-600:]}")
    # Fails if no aborted record names the signal and the phase it interrupted.
    _check(f"{label}: aborted record with signal and interrupted_phase",
           final.get("phase") == "aborted" and final.get("signal") == "SIGTERM"
           and (final.get("interrupted_phase") == phase if phase else
                final.get("interrupted_phase") in ("starting", "between_calls"))
           and not any(k in final for k in pg._CALL_FIELDS), f"{final}")
    # Fails if finalization ran despite the latch: an attestation, an export or a
    # delivery into the operator's tree.
    status = _git(repo, "status", "--porcelain")
    _check(f"{label}: no station-run.json, no export, nothing delivered",
           not (rd / sr.STATION_RUN_FILENAME).exists()
           and not (out / MANIFEST_FILENAME).exists() and status == "", f"{status!r}")
    ok, detail = _no_worktree_left(repo)
    # Fails if cmd_fix's cleanup did not remove the worktree on this abort.
    _check(f"{label}: no worktree or .git/worktrees entry remains", ok, detail)
    return final


def _implement():
    Path("feature.txt").write_text("new\n")
    return "implemented"


def test_sigterm_in_gate_aborts():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        (repo / ".claude" / "verify.sh").write_text(_SLOW_GATE)
        _git(repo, "commit", "-qam", "slow gate")
        rd, out, pids = tmp / "run", tmp / "export", tmp / "gate_pids"
        os.environ["POTLUCK_GATE_PIDS"] = str(pids)
        seen = {}

        def watch():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    rec = _read(rd / pg.FILENAME)
                except (OSError, ValueError):
                    rec = {}
                if (rec.get("phase") == "in_call"
                        and str(rec.get("step", "")).startswith("gate:")
                        and pids.exists() and pids.read_text()):
                    seen.update(rec)
                    _signal_main()
                    return
                time.sleep(0.05)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            code, err = _run_fix(repo, _Doer(_implement), [
                "--run-dir", str(rd), "--export-dir", str(out), "--export-task", "t"])
        finally:
            watcher.join(5)
            os.environ.pop("POTLUCK_GATE_PIDS", None)
        gate_pids = [int(p) for p in pids.read_text().split()] if pids.exists() else []
        survivors = [ln for p in gate_pids for ln in _alive_in_group(p)]
        for p in gate_pids:            # a failing run must not leak its gate
            try:
                os.killpg(p, signal.SIGKILL)
            except OSError:
                pass
        # Fails if the test never reached the gate, so nothing below tests it.
        _check("gate-abort: SIGTERM was sent while in_call on the gate",
               seen.get("phase") == "in_call" and gate_pids, f"{seen}")
        _check_aborted("gate-abort", rd, out, repo, code, err, "in_call")
        # Fails if cancelling the gate call does not tear its process group down.
        _check("gate-abort: no gate process survives", not survivors, f"{survivors}")


def test_sigterm_in_checkpoint_write_aborts():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        rd, out = tmp / "run", tmp / "export"
        real_write = ck.Checkpointer.write
        writes = []

        def signalled(self, *a, **kw):
            writes.append(_read(rd / pg.FILENAME)["phase"])
            if len(writes) == 1:
                _signal_main()          # handled here, mid-write, on the loop thread
            return real_write(self, *a, **kw)
        ck.Checkpointer.write = signalled
        try:
            code, err = _run_fix(repo, _Doer(_implement), [
                "--run-dir", str(rd), "--export-dir", str(out), "--export-task", "t"])
        finally:
            ck.Checkpointer.write = real_write
        # Fails if the test never reached a checkpoint write.
        _check("checkpoint-abort: SIGTERM was sent inside a checkpoint write",
               bool(writes), f"{writes}")
        _check_aborted("checkpoint-abort", rd, out, repo, code, err,
                       writes[0] if writes else None)
        # Fails if the signal tore the synchronous write (it must run to the end)
        # or a further checkpoint was written after the latch.
        try:
            ck.load_checkpoint(rd)
            whole = True
        except Exception as e:  # noqa: BLE001 -- any failure means torn
            whole = f"{type(e).__name__}: {e}"
        _check("checkpoint-abort: the interrupted write completed whole, no more follow",
               whole is True and len(writes) == 1, f"{whole} writes={writes}")


# --- the latch on every exit path, and the terminal write ---------------------

def test_signal_then_startup_failure_aborts():
    for label, where in (("startup-refusal", "startup"), ("cleanup", "cleanup")):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            repo = _setup(tmp)
            rd, out = tmp / "run", tmp / "export"
            real_build, real_remove = cli._build_checkpointer, cli._RunWorktree.remove

            def refuse(*a, **kw):
                if where == "startup":
                    _signal_main()
                print("Error: refused", file=sys.stderr)
                sys.exit(1)

            def remove(self):
                if where == "cleanup":
                    _signal_main()
                return real_remove(self)
            cli._build_checkpointer, cli._RunWorktree.remove = refuse, remove
            try:
                code, err = _run_fix(repo, _Doer(_implement), ["--run-dir", str(rd)])
            finally:
                cli._build_checkpointer, cli._RunWorktree.remove = real_build, real_remove
            # Fails if the refusal's exit 1 escapes and the record says done
            # although a signal was latched before the commit boundary.
            _check_aborted(f"{label}+signal", rd, out, repo, code, err, "starting")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        rd = tmp / "run"
        real_build = cli._build_checkpointer

        def refuse(*a, **kw):
            sys.exit(1)
        cli._build_checkpointer = refuse
        try:
            code, _ = _run_fix(repo, _Doer(_implement), ["--run-dir", str(rd)])
        finally:
            cli._build_checkpointer = real_build
        final = _read(rd / pg.FILENAME)
        # Fails if reconciling the latch changed an unsignalled refusal.
        _check("startup-refusal, no signal: exit 1 and a done record",
               code == 1 and final["phase"] == "done" and "signal" not in final
               and "late_signal" not in final, f"code={code} {final}")


def test_signal_during_finish_is_recorded():
    for label in ("heartbeat join", "terminal write"):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            repo = _setup(tmp)
            rd = tmp / "run"
            real_start, real_write = pg.ProgressWriter.start, pg.ProgressWriter._write_locked
            sent = []

            def start(self):
                real_start(self)
                if label == "heartbeat join":
                    thread_join = self._thread.join

                    def join(*a, **kw):
                        sent.append(True)
                        _signal_main()
                        return thread_join(*a, **kw)
                    self._thread.join = join

            def write(self):
                real_write(self)
                if (label == "terminal write" and self._state["phase"] == "done"
                        and not sent):
                    sent.append(True)
                    _signal_main()      # after the record hit disk, before commit
            pg.ProgressWriter.start, pg.ProgressWriter._write_locked = start, write
            try:
                code, err = _run_fix(repo, _Doer(_implement), ["--run-dir", str(rd)])
            finally:
                pg.ProgressWriter.start = real_start
                pg.ProgressWriter._write_locked = real_write
            final = _read(rd / pg.FILENAME)
            # Fails if the terminal fields are snapshotted before the heartbeat is
            # joined or before the record is written, so a first signal landing
            # there is lost from the record.
            _check(f"finish ({label}): done records late_signal",
                   code == 0 and sent and final["phase"] == "done"
                   and final.get("late_signal") == "SIGTERM",
                   f"code={code} {final} {err[-300:]}")


def test_exit_3_has_one_producer():
    import re
    src = {p.name: p.read_text() for p in Path(cli.__file__).parent.glob("*.py")
           if not p.name.startswith("test_")}
    literal = [n for n, s in src.items() if re.search(r"exit\(\s*3\s*\)|return 3\b", s)]
    uses = sum(s.count("sys.exit(ABORT_EXIT_STATUS)") for s in src.values())
    # Fails if any other path exits 3, or the abort stops using the named status.
    _check("exit 3: only the abort path produces it",
           cli.ABORT_EXIT_STATUS == 3 and not literal and uses == 1,
           f"literal={literal} uses={uses}")


def main() -> bool:
    for t in (test_exit_3_has_one_producer,
              test_writer_atomic_seq_phases_deadlines,
              test_writer_failed_rename_keeps_last_record,
              test_seq_continues_across_writers,
              test_late_signal_after_finish_is_amended,
              test_heartbeat_while_loop_thread_blocked,
              test_lifecycle_fields_and_phases,
              test_bare_fix_writes_no_progress,
              test_late_signal_keeps_publication,
              test_sigterm_in_call_aborts,
              test_sigterm_in_gate_aborts,
              test_sigterm_in_checkpoint_write_aborts,
              test_signal_then_startup_failure_aborts,
              test_signal_during_finish_is_recorded):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
