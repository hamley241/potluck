"""Tests for the conductor §5 `potluck fix --gate-file` seam — running the
verification gate from an EXTERNAL path outside the working tree so the product tree
stays clean and the gate is never an unattributed commit.

Real subprocess for gate execution + real git for the resume paths (the seams a stub
cannot reach). The resume tests assert the SPECIFIC fail-closed diagnostic and rely on
control-flow ordering (the gate guard runs BEFORE checkpoint load), so a removed guard
would surface a DIFFERENT downstream error — not a false pass (codex §5 r1 MAJOR).

Runnable: `python3 -m harness.test_gate_file`.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import checkpoint as ck
from harness.cli import (
    _FROZEN_GATE_NAME,
    _refuse_gate_inside_tree,
    _require_gate,
    _resolve_resume_gate,
    build_orchestrator,
    build_parser,
    cmd_fix,
    load_config,
)
from harness.orchestrator import GateResult, real_run_gate
from harness.schemas import ExecCursor, Phase, TreeSnapshot
from harness.station_run import gate_digest_of_bytes

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _init_repo(root: Path) -> None:
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *a], cwd=root, check=True)
    (root / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)


def _run_capturing(fn):
    """Run fn(); return (exit_code_or_None, stderr_text). exit_code None == no exit."""
    err = io.StringIO()
    code = None
    with contextlib.redirect_stderr(err):
        try:
            fn()
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


def load_config_default():
    ns = build_parser().parse_args(["fix", "--spec", "x"])
    return load_config(ns)


def _fix_args(*argv):
    return build_parser().parse_args(["fix", *argv])


# --- build_orchestrator wiring (pure: cfg -> object, no I/O) ---

def test_build_orchestrator_wires_external_gate():
    cfg = load_config_default()
    _check("default_gate_is_real_run_gate",
           build_orchestrator(cfg).run_gate is real_run_gate)
    g = build_orchestrator(cfg, gate_path="/abs/handoff/verify.sh").run_gate
    _check("external_gate_is_partial",
           isinstance(g, functools.partial) and g.func is real_run_gate
           and g.args == ("/abs/handoff/verify.sh",), f"got {g!r}")


# --- _require_gate: external present/missing, in-tree fallback ---

def test_require_gate_external_and_intree():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ext = root / "outside" / "verify.sh"
        _write(ext, "#!/bin/sh\nexit 0\n")
        _check("require_external_present_ok", _run_capturing(
            lambda: _require_gate(str(ext)))[0] is None)
        _check("require_external_missing_exits", _run_capturing(
            lambda: _require_gate(str(root / "nope.sh")))[0] is not None)
        cwd0 = os.getcwd()
        os.chdir(root)
        try:
            _check("require_intree_missing_exits",
                   _run_capturing(lambda: _require_gate(None))[0] is not None)
            _write(root / ".claude" / "verify.sh", "#!/bin/sh\nexit 0\n")
            _check("require_intree_present_ok",
                   _run_capturing(lambda: _require_gate(None))[0] is None)
        finally:
            os.chdir(cwd0)


# --- real_run_gate executes the EXTERNAL file (the one that wins over a failing in-tree) ---

def test_real_run_gate_runs_external_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ext = root / "handoff" / "verify.sh"
        _write(ext, "#!/bin/sh\necho EXTERNAL_GATE_MARKER\nexit 0\n")
        cwd0 = os.getcwd()
        os.chdir(root)
        try:
            _write(root / ".claude" / "verify.sh", "#!/bin/sh\necho intree\nexit 1\n")
            res = GateResult.from_json_str(asyncio.run(real_run_gate(str(ext))))
            _check("external_gate_ok", res.ok, f"exit={res.exit_code}")
            _check("external_gate_output_marker",
                   "EXTERNAL_GATE_MARKER" in res.output, res.output[:120])
            extfail = root / "handoff" / "fail.sh"
            _write(extfail, "#!/bin/sh\nexit 1\n")
            res2 = GateResult.from_json_str(asyncio.run(real_run_gate(str(extfail))))
            _check("external_gate_fail_red", not res2.ok and res2.exit_code == 1)
        finally:
            os.chdir(cwd0)


# --- the gate binding lives in the TAMPER-EVIDENT identity, not editable run_inputs ---

def _mk_tree():
    return TreeSnapshot(repo_base_commit="base", work_tree_oid="w",
                        index_tree_oid="i", tree_snapshot_digest="td")


class _FakeStore:
    """A minimal checkpoint store (the test_resume pattern): fixed tree, records the
    written checkpoint, replays it on load — lets us exercise the REAL Checkpointer
    write/load_and_verify (build_identity + verify_identity) with no git."""
    def __init__(self, tree):
        self._tree = tree
        self.saved = None

    def snapshot(self):
        return self._tree

    def write(self, ckpt):
        self.saved = ckpt

    def load(self):
        if self.saved is None:
            raise ck.CheckpointError("no checkpoint")
        return self.saved

    def restore(self, tree):
        pass


def _write_ckpt(store, gate_binding):
    cp = ck.Checkpointer(store, "s", "a", "cfg", "m", gate_binding_digest=gate_binding)
    cp.write(ExecCursor(phase=Phase.IMPLEMENT), rounds_used=0, state={})


def _resume_verify(store, gate_binding):
    """(ok, err): a resuming Checkpointer with `gate_binding` runs load_and_verify."""
    cp = ck.Checkpointer(store, "s", "a", "cfg", "m", gate_binding_digest=gate_binding)
    try:
        cp.load_and_verify()
        return True, ""
    except ck.CheckpointError as e:
        return False, str(e)


def test_identity_binds_gate_downgrade_refused():
    # An external run checkpoints with gate_binding = the frozen digest. A resume that
    # resolves the IN-TREE sentinel (the frozen copy deleted — the codex downgrade) has
    # a different binding, so verify_identity REFUSES. This is anchored in the identity,
    # not the editable run_inputs.json (codex §5 r2 BLOCKING).
    store = _FakeStore(_mk_tree())
    _write_ckpt(store, gate_binding="EXTERNAL_FROZEN_DIGEST")
    ok, err = _resume_verify(store, gate_binding=ck.INTREE_GATE_BINDING)
    _check("downgrade_refused", not ok and "gate binding" in err, err)


def test_identity_binds_gate_tamper_refused():
    # The frozen gate edited after checkpoint -> its recomputed digest differs -> refuse.
    store = _FakeStore(_mk_tree())
    _write_ckpt(store, gate_binding="DIGEST_A")
    ok, err = _resume_verify(store, gate_binding="DIGEST_B")
    _check("tamper_refused", not ok and "gate binding" in err, err)


def test_identity_matching_gate_passes():
    # Same gate binding on both passes -> identity verifies (not a blanket refusal).
    store = _FakeStore(_mk_tree())
    _write_ckpt(store, gate_binding="DIGEST_A")
    ok, err = _resume_verify(store, gate_binding="DIGEST_A")
    _check("matching_gate_passes", ok, err)


# --- resume gate RESOLUTION is a file fact (frozen presence), not a mutable field ---

def test_resolve_resume_gate_mode_from_presence():
    with tempfile.TemporaryDirectory() as d:
        run_dir = Path(d) / "run"
        run_dir.mkdir()
        # no frozen copy -> in-tree sentinel, no path
        path, binding = _resolve_resume_gate(str(run_dir))
        _check("resolve_absent_is_intree",
               path is None and binding == ck.INTREE_GATE_BINDING)
        # frozen copy present -> external: its path + its digest
        gate_bytes = b"#!/bin/sh\nexit 0\n"
        (run_dir / _FROZEN_GATE_NAME).write_bytes(gate_bytes)
        path2, binding2 = _resolve_resume_gate(str(run_dir))
        _check("resolve_present_is_external",
               path2 == str(run_dir / _FROZEN_GATE_NAME)
               and binding2 == gate_digest_of_bytes(gate_bytes))


# --- --gate-file front-door validation (requires --run-dir, refuses in-tree) ---

def test_gate_file_requires_run_dir():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        _init_repo(root)
        ext = Path(d) / "gate" / "verify.sh"
        _write(ext, "#!/bin/sh\nexit 0\n")
        cwd0 = os.getcwd()
        os.chdir(root)
        try:
            code, err = _run_capturing(
                lambda: cmd_fix(_fix_args("--spec", "x", "--gate-file", str(ext))))
            _check("gate_file_needs_run_dir",
                   code == 1 and "requires --run-dir" in err, err[:160])
        finally:
            os.chdir(cwd0)


def test_refuse_gate_inside_tree():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        _init_repo(root)
        intree = root / "scripts" / "verify.sh"
        _write(intree, "#!/bin/sh\nexit 0\n")
        cwd0 = os.getcwd()
        os.chdir(root)
        try:
            code, err = _run_capturing(lambda: _refuse_gate_inside_tree(str(intree)))
            _check("refuse_intree_gate",
                   code is not None and "INSIDE the git working tree" in err, err[:160])
            # an outside gate is accepted (no exit)
            outside = Path(d) / "outside" / "verify.sh"
            _write(outside, "#!/bin/sh\nexit 0\n")
            _check("accept_outside_gate",
                   _run_capturing(lambda: _refuse_gate_inside_tree(str(outside)))[0]
                   is None)
        finally:
            os.chdir(cwd0)


def main() -> bool:
    for t in (test_build_orchestrator_wires_external_gate,
              test_require_gate_external_and_intree,
              test_real_run_gate_runs_external_file,
              test_identity_binds_gate_downgrade_refused,
              test_identity_binds_gate_tamper_refused,
              test_identity_matching_gate_passes,
              test_resolve_resume_gate_mode_from_presence,
              test_gate_file_requires_run_dir,
              test_refuse_gate_inside_tree):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
