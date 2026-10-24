"""Tests for the explicit DELIVERY of a selected result (ruling 001, part 3 of 3).

Round 0 (and round 1) are genuine exports from the REAL `cmd_fix` path with only the
doer swapped (the `test_run_worktree` harness); `potluck deliver` is then driven through
the real parser. Every negative is a constructed fixture that must be refused, NAMING
the reason, with the operator's HEAD, refs, index and every file byte-identical
afterwards and the export directory retained unchanged:

  * A5: a digest that is not the directory's manifest's; A6: no model call.
  * A7: an empty commit on top; A8: a staged change the working tree reverses;
    A9: a foreign edit to an UNRELATED file the patch would apply over, and an
    untracked file; A10: an ignored path the change would overwrite.
  * A11/A12/A13: push URL and root-commit mismatches, a null push URL, a renamed
    checkout that still delivers.
  * A14/A16: git apply's own all-or-nothing refusal, reported as a PATCH CONFLICT
    distinct from baseline drift; A17: the refusal names the durable export.
  * A18: round 0 exports, round 1 imports and exports, round 1's digest delivers
    both rounds' work once; a second delivery refuses. A19: --import-dir without
    --export-dir is refused at validation. A3: a plain run still delivers directly.
  * A23: no worktree survives any path.

Runnable: `python3 -m harness.test_deliver`.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import harness.cli as cli
from harness import export as ex
from harness import station_run as sr
from harness.test_export import _export_args, _operator_state, _rich_change, _setup_rich
from harness.test_run_worktree import _Doer, _git, _no_worktree_left, _run_fix

_results: list[tuple[str, bool, str]] = []
_URL = "https://example.invalid/o/repo.git"


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _dir_state(d: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.iterdir()) if p.is_file()}


def _deliver(repo: Path, export_dir: Path, digest: str,
             json_output: bool = False) -> tuple[int | None, str, str]:
    """`potluck deliver` from `repo`. Returns (exit code, stderr, stdout)."""
    cwd0 = os.getcwd()
    err, out, code = io.StringIO(), io.StringIO(), None
    argv = ["deliver", "--export-dir", str(export_dir), "--manifest-digest", digest]
    try:
        os.chdir(repo)
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                args = cli.build_parser().parse_args(
                    argv + (["--json-output"] if json_output else []))
                args.func(args)
            except SystemExit as e:
                code = e.code
    finally:
        os.chdir(cwd0)
    return code, err.getvalue(), out.getvalue()


def _round0(tmp: Path) -> tuple[Path, Path, dict, str]:
    repo = _setup_rich(tmp)
    _git(repo, "remote", "add", "origin", _URL)
    out0 = tmp / "export0"
    code, err = _run_fix(repo, _Doer(_rich_change), _export_args(tmp, out0))
    assert code == 0, err[-600:]
    m, d = ex.load_export(out0)
    return repo, out0, m, d


def _rewrite(src: Path, dst: Path, **repository) -> str:
    """Re-export `src` with a different recorded repository identity, keeping manifest,
    artifact and payloads consistent -- so only delivery's identity check can refuse."""
    m, _ = ex.load_export(src)
    return ex.write_export(
        dst, task_id=m["task_id"], run_id=m["run_id"], round_index=m["round"],
        repository={**m["repository"], **repository}, base_commit=m["base_commit"],
        output_tree_oid=m["output_tree_oid"], output_tree_digest=m["output_tree_digest"],
        patch=(src / ex.PATCH_FILENAME).read_bytes(),
        station_run_bytes=(src / sr.STATION_RUN_FILENAME).read_bytes(),
        spec=(src / ex.SPEC_FILENAME).read_text(),
        acceptance=(src / ex.ACCEPTANCE_FILENAME).read_text(),
        gate_bytes=(src / ex.GATE_FILENAME).read_bytes())


def _refusal(name: str, prepare, *needles, kind: str):
    """Build round 0, let `prepare(repo, out0, m, d, tmp)` disturb the checkout and/or
    return (export_dir, digest); delivery must refuse by name and write nothing."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0, m, d = _round0(tmp)
        export_dir, digest = prepare(repo, out0, m, d, tmp) or (out0, d)
        before, kept = _operator_state(repo), _dir_state(export_dir)
        held = ex.load_export(export_dir)[1]
        code, err, out = _deliver(repo, export_dir, digest, json_output=True)
        wanted = [n(m, repo) if callable(n) else n for n in needles]
        missing = [n for n in wanted if n not in err]
        _check(f"{name}: refused, naming the reason (A2)", code == 1 and not missing,
               f"code={code} missing={missing} err={err[-800:]}")
        try:
            structured = json.loads(out)
        except ValueError:
            structured = {}
        _check(f"{name}: the structured refusal names its kind ({kind})",
               structured.get("outcome") == "refused"
               and structured.get("refusal_kind") == kind
               and structured.get("retained_result") == str(export_dir), out[-400:])
        _check(f"{name}: HEAD, refs, index and every file byte-identical (A15)",
               _operator_state(repo) == before)
        _check(f"{name}: the result is RETAINED, unchanged, and named (A15 A16 A17)",
               _dir_state(export_dir) == kept and ex.load_export(export_dir)[1] == held
               and f"RETAINED, unchanged, at {export_dir}" in err
               and "potluck-unapplied-" not in err)
        _check(f"{name}: never claims the tree 'changed during the run' (A16)",
               "changed during the run" not in err)
        ok, detail = _no_worktree_left(repo)
        _check(f"{name}: no worktree survives (A23)", ok, detail)


def _empty_commit(repo, out0, m, d, tmp):
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")


def _staged_then_reverted(repo, out0, m, d, tmp):
    (repo / "base.txt").write_text("staged\n")
    _git(repo, "add", "base.txt")
    (repo / "base.txt").write_text("base\n")
    # The proved-reachable fixture: the add-A digest IS the base tree's, the index is not.
    from harness import checkpoint as ck
    base_tree = _git(repo, "rev-parse", f"{m['base_commit']}^{{tree}}")
    _check("fixture (A8): the add-A digest equals base^{tree}'s while the index has M",
           sr._output_tree_digest(repo) == ck._tree_digest(repo, base_tree)
           and _git(repo, "diff", "--cached", "--name-status", "HEAD") == "M\tbase.txt")


def _foreign_edit(repo, out0, m, d, tmp):
    # .gitignore is untouched by the patch, so `git apply` WOULD succeed over this.
    with open(repo / ".gitignore", "a") as f:
        f.write("# a foreign operator edit\n")
    patch = (out0 / ex.PATCH_FILENAME).read_bytes()
    check = subprocess.run(["git", "apply", "--check", "--binary", "-"], cwd=repo,
                           input=patch, capture_output=True)
    _check("fixture (A9): the patch WOULD apply over the foreign edit",
           check.returncode == 0, check.stderr.decode()[-300:])


def _untracked(repo, out0, m, d, tmp):
    (repo / "notes.txt").write_text("operator scratch\n")


def _ignored_in_the_way(repo, out0, m, d, tmp):
    # Ignored via .git/info/exclude, so the baseline digest is still base^{tree}.
    with open(repo / ".git" / "info" / "exclude", "a") as f:
        f.write("feature.txt\n")
    (repo / "feature.txt").write_text("the operator's own, ignored\n")


def _wrong_digest(repo, out0, m, d, tmp):
    return out0, "0" * 64


def _other_url(repo, out0, m, d, tmp):
    _git(repo, "remote", "set-url", "origin", "https://example.invalid/fork/repo.git")


def _other_roots(repo, out0, m, d, tmp):
    bad = tmp / "bad"
    return bad, _rewrite(out0, bad, root_commits=["e" * 40])


def _null_url(repo, out0, m, d, tmp):
    bad = tmp / "bad"
    return bad, _rewrite(out0, bad, push_url_raw=None)


def test_refusals():
    """A5 A7 A8 A9 A10 A11 A12, each with A15/A16/A17/A23."""
    _refusal("wrong digest (A5)", _wrong_digest, "0" * 64,
             lambda m, r: ex.load_export(r.parent / "export0")[1],
             "pathname alone never authorizes delivery", kind="selection")
    _refusal("empty commit on top (A7)", _empty_commit, "BASELINE DRIFT",
             "HEAD is not the result's base_commit", lambda m, r: m["base_commit"],
             lambda m, r: _git(r, "rev-parse", "HEAD"), kind="baseline_drift")
    _refusal("staged change the tree reverses (A8)", _staged_then_reverted,
             "BASELINE DRIFT", "real index", "M\tbase.txt", kind="baseline_drift")
    _refusal("foreign edit to an UNRELATED file, patch applies (A9)", _foreign_edit,
             "BASELINE DRIFT", "even if the patch applies", "working_tree_digest",
             kind="baseline_drift")
    _refusal("untracked file (A9)", _untracked, "BASELINE DRIFT",
             "untracked-non-ignored", kind="baseline_drift")
    _refusal("ignored-path obstruction (A10)", _ignored_in_the_way,
             "IGNORED-PATH OBSTRUCTION", "feature.txt", kind="ignored_obstruction")
    _refusal("push URL mismatch (A11)", _other_url, "REPOSITORY IDENTITY", _URL,
             "https://example.invalid/fork/repo.git", kind="identity")
    _refusal("root commit mismatch (A11)", _other_roots, "REPOSITORY IDENTITY",
             "e" * 40, lambda m, r: m["repository"]["root_commits"][0], kind="identity")
    _refusal("null push_url_raw (A12)", _null_url, "push_url_raw",
             "equality against null is vacuous", kind="identity")


def test_patch_conflict_is_all_or_nothing():
    """A14 + A16: with the named ignored-path check bypassed, `git apply` itself refuses
    a multi-file patch with one obstructed path and writes NONE of it -- reported as a
    PATCH CONFLICT, distinct from baseline drift."""
    real = subprocess.run

    def no_ignored(cmd, *a, **kw):
        if cmd[:2] == ["git", "ls-files"] and "--ignored" in cmd:
            return real(["true"], capture_output=True)
        return real(cmd, *a, **kw)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0, m, d = _round0(tmp)
        _ignored_in_the_way(repo, out0, m, d, tmp)
        before, kept = _operator_state(repo), _dir_state(out0)
        try:
            subprocess.run = no_ignored
            code, err, _ = _deliver(repo, out0, d)
        finally:
            subprocess.run = real
        _check("conflict: refused as a PATCH CONFLICT, not drift (A16)",
               code == 1 and "PATCH CONFLICT" in err and "BASELINE DRIFT" not in err
               and "feature.txt" in err, f"code={code} err={err[-600:]}")
        _check("conflict: nothing applied -- all-or-nothing (A14)",
               _operator_state(repo) == before and not (repo / "blob.bin").exists()
               and (repo / "gone.txt").exists())
        _check("conflict: the result is retained and named (A15 A17)",
               _dir_state(out0) == kept and f"RETAINED, unchanged, at {out0}" in err)


def test_round_trip_and_once():
    """A18 + A6 + A13 + A14 + A23: round 1's digest delivers both rounds' work once."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0, m0, d0 = _round0(tmp)
        out1 = tmp / "export1"

        def more():
            Path("round1.txt").write_text("round 1\n")
            Path("feature.txt").write_text("revised in round 1\n")
            return "done"
        code, err = _run_fix(repo, _Doer(more), [
            "--import-dir", str(out0), "--run-dir", str(tmp / "run1"),
            "--export-dir", str(out1), "--export-task", "task-42", "--export-round", "1"])
        assert code == 0, err[-600:]
        m1, d1 = ex.load_export(out1)
        _check("round trip: round 1 delivered nothing itself",
               not (repo / "round1.txt").exists() and not (repo / "feature.txt").exists())
        # A13: rename the checkout; the basename is display, never identity.
        renamed = tmp / "renamed-checkout"
        repo.rename(renamed)
        refs0, index0 = _git(renamed, "for-each-ref"), _git(renamed, "ls-files", "-s")
        head0 = _git(renamed, "rev-parse", "HEAD")
        # A6: delivery must not build an orchestrator, a doer, a reviewer or a gate.
        dispatched = []

        def trap(name):
            def f(*a, **kw):
                dispatched.append(name)
                raise AssertionError(f"delivery dispatched {name}")
            return f
        saved = {n: getattr(cli, n) for n in ("build_orchestrator", "RealDoerClient",
                                              "ReviewerClient", "TiebreakerClient",
                                              "real_run_gate")}
        try:
            for n in saved:
                setattr(cli, n, trap(n))
            code, err, out = _deliver(renamed, out1, d1)
        finally:
            for n, v in saved.items():
                setattr(cli, n, v)
        _check("round trip: round 1's digest delivers (A18), renamed checkout (A13)",
               code == 0 and "delivered result" in out, f"code={code} err={err[-600:]}")
        _check("round trip: no model call, gate or orchestrator dispatched (A6)",
               not dispatched, f"{dispatched}")
        _check("round trip: both rounds' complete candidate landed (A18)",
               (renamed / "round1.txt").read_text() == "round 1\n"
               and (renamed / "feature.txt").read_text() == "revised in round 1\n"
               and (renamed / "blob.bin").exists() and not (renamed / "gone.txt").exists()
               and (renamed / "secret.env").read_text() == "TOKEN=keep-me\n")
        _check("round trip: the tree matches round 1's attested output digest (A18)",
               sr._output_tree_digest(renamed) == m1["output_tree_digest"])
        _check("round trip: no ref written, real index untouched, HEAD unmoved (A14)",
               _git(renamed, "for-each-ref") == refs0
               and _git(renamed, "ls-files", "-s") == index0
               and _git(renamed, "rev-parse", "HEAD") == head0)
        state, kept = _operator_state(renamed), _dir_state(out1)
        code, err, _ = _deliver(renamed, out1, d1)
        _check("once: a second delivery of the same digest refuses as drift (A18 A21)",
               code == 1 and "BASELINE DRIFT" in err and _operator_state(renamed) == state
               and _dir_state(out1) == kept, f"code={code} err={err[-400:]}")
        code, err, _ = _deliver(renamed, out0, d0)
        _check("once: round 0's result cannot land over round 1's either",
               code == 1 and "BASELINE DRIFT" in err and _operator_state(renamed) == state)
        ok, detail = _no_worktree_left(renamed)
        _check("round trip: no worktree survives (A23)", ok, detail)


def test_import_without_export_refused():
    """A19: refused at argument validation, naming both flags and the recovery."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0, _, _ = _round0(tmp)
        before = _operator_state(repo)
        doer = _Doer(lambda: (Path("x.txt").write_text("x\n"), "done")[1])
        code, err = _run_fix(repo, doer, ["--import-dir", str(out0)])
        _check("import-only: refused naming both flags and the recovery (A19)",
               code == 1 and "--import-dir requires --export-dir" in err
               and "potluck deliver" in err and "--manifest-digest" in err,
               f"code={code} err={err[-400:]}")
        _check("import-only: no doer call, nothing written (A19)",
               not doer.cwds and _operator_state(repo) == before)
        ok, detail = _no_worktree_left(repo)
        _check("import-only: no worktree survives (A23)", ok, detail)


def test_plain_run_still_delivers_directly():
    """A3: no import, no export, no delivery flag -- delivers on PASSED as before."""
    from harness.test_run_worktree import _setup
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        code, err = _run_fix(repo, _Doer(lambda: (Path("f.txt").write_text("f\n"),
                                                  "done")[1]))
        _check("plain: a passed run still delivers into the tree (A3)",
               code == 0 and (repo / "f.txt").read_text() == "f\n", err[-400:])
        code, err = _run_fix(repo, _Doer(lambda: "nothing"))
        _check("plain: a non-passed run writes nothing (A3)", code == 1
               and _git(repo, "status", "--porcelain") == "?? f.txt")
    src = inspect.getsource(cli._deliver_selected) + inspect.getsource(cli.cmd_deliver)
    _check("mkstemp: the delivery path has no temp-file copy (A17)",
           "mkstemp" not in src and "potluck-unapplied" not in src)
    _check("once: the contract states what 'once' does NOT promise (A21)",
           "Durable exactly-once acceptance" in src and "caller's" in src)


def test_write_phase_failure_is_not_reported_as_nothing_written():
    """codex r1 MAJOR: `git apply` checks applicability BEFORE writing, but its write
    phase is NOT transactional -- `write_out_one_result`/`write_out_results` in apply.c
    remove and recreate files sequentially and return on a write error without rolling
    earlier files back. So a nonzero exit does not prove nothing was written, and
    reporting `patch_conflict` ("wrote nothing") would be a false assurance in the one
    message the operator acts on.

    The fixture: two tracked files, the second inside a readable but NON-WRITABLE
    directory. Every baseline check passes (a directory's mode is not in the tree), the
    first file is written, the second fails."""
    from harness.test_run_worktree import _setup
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        (repo / "a.txt").write_text("a\n")
        (repo / "sub").mkdir()
        (repo / "sub" / "b.txt").write_text("b\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "two files")
        _git(repo, "remote", "add", "origin", _URL)

        def touch_both():
            Path("a.txt").write_text("a2\n")
            Path("sub/b.txt").write_text("b2\n")
            return "done"

        out0 = tmp / "export0"
        code, err = _run_fix(repo, _Doer(touch_both), _export_args(tmp, out0))
        _check("write-phase: the two-file round exported", code == 0, f"{code} {err[-300:]}")
        m, d = ex.load_export(out0)
        patch = (out0 / ex.PATCH_FILENAME).read_text()
        _check("write-phase: the patch touches a.txt before sub/b.txt",
               patch.index("a.txt") < patch.index("sub/b.txt"))
        before = _operator_state(repo)
        (repo / "sub").chmod(0o500)
        try:
            code, err, _out = _deliver(repo, out0, d)
        finally:
            (repo / "sub").chmod(0o700)
        _check("write-phase: refused", code == 1, f"code={code}")
        _check("write-phase: NOT reported as patch_conflict / 'wrote nothing'",
               "wrote nothing" not in err and "PATCH CONFLICT" not in err, err[-500:])
        _check("write-phase: named PARTIAL APPLICATION, and says the write phase is "
               "not transactional",
               "PARTIAL APPLICATION" in err and "not transactional" in err, err[-500:])
        _check("write-phase: the restore returned the checkout to the base",
               _operator_state(repo) == before,
               "a.txt=%r" % (repo / "a.txt").read_text())
        _check("write-phase: the result is retained and named",
               str(out0) in err and ex.load_export(out0)[1] == d)


def main() -> bool:
    for t in (test_refusals, test_patch_conflict_is_all_or_nothing,
              test_round_trip_and_once, test_import_without_export_refused,
              test_plain_run_still_delivers_directly,
              test_write_phase_failure_is_not_reported_as_nothing_written):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
