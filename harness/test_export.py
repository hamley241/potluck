"""Tests for the station EXPORT (ruling 001, part 1 of 3).

Drives the REAL `cmd_fix` path against a real temp git repo with only the doer
swapped (the `test_run_worktree` harness), plus `export.load_export` over
constructed failure fixtures:

  * A5/A6: a PASSED run told to export writes the manifest, the cumulative patch,
    station-run.json and the exact spec/acceptance/gate inputs, each digested in
    the manifest under a manifest-relative name; the imported-result field is an
    explicit null.
  * A7: the operator's tree, index, HEAD and every ref are byte-identical after.
  * A8/A9: a missing manifest and a flipped payload byte are refused, naming why;
    so are a missing input payload, a missing payload-table entry and a missing task.
  * A10: the patch carries additions, deletions, an exec bit, a symlink, binary
    bytes and an untracked file, leaves ignored paths out, and reproduces the
    manifest's output tree digest on a fresh checkout of the base.
  * A11: an unwritable export dir makes a PASSED run FAIL -- exit code, JSON and
    human summary alike, with the station's passed outcome kept separately.
  * A12: a non-PASSED run leaves no acceptable export (a stale one is cleared).
  * A13: a doer that commits makes the run ineligible to export, reason intact.
  * A4/A14: --allow-dirty's help and effects, and station-run v1's fields, pinned.
  * A15: no worktree survives any of these paths.

Runnable: `python3 -m harness.test_export`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import harness.cli as cli
from harness import checkpoint as ck
from harness import export as ex
from harness import station_run as sr
from harness.test_run_worktree import _Doer, _git, _no_worktree_left, _run_fix, _setup

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _operator_state(repo: Path) -> dict:
    """Every observable the export must not write: refs, HEAD, index, and the bytes
    and modes of every file in the working tree (ignored ones included)."""
    files = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts:
            continue
        if p.is_symlink():
            files[str(p)] = ("link", os.readlink(p))
        elif p.is_file():
            files[str(p)] = (oct(p.stat().st_mode), hashlib.sha256(p.read_bytes()).hexdigest())
    return {"refs": _git(repo, "for-each-ref"), "head": _git(repo, "rev-parse", "HEAD"),
            "symref": _git(repo, "symbolic-ref", "HEAD"),
            "index": _git(repo, "ls-files", "-s"),
            "status": _git(repo, "status", "--porcelain", "--ignored", "-uall"),
            "files": files}


def _rich_change():
    """Additions, a deletion, an exec-bit change, a symlink, binary bytes, an
    untracked file, and an ignored file that must stay out."""
    Path("feature.txt").write_text("new\n")
    Path("base.txt").chmod(0o755)
    Path("gone.txt").unlink()
    os.symlink("feature.txt", "link")
    Path("blob.bin").write_bytes(bytes(range(256)) * 4)
    Path("secret.env").write_text("TOKEN=doer-ignored\n")
    return "implemented"


def _setup_rich(tmp: Path) -> Path:
    repo = _setup(tmp)
    (repo / "gone.txt").write_text("delete me\n")
    _git(repo, "add", "gone.txt")
    _git(repo, "commit", "-qm", "gone")
    return repo


def _export_args(tmp: Path, out: Path):
    return ["--run-dir", str(tmp / "run"), "--export-dir", str(out),
            "--export-task", "task-42", "--export-round", "3"]


def _refused(out: Path, *needles: str) -> tuple[bool, str]:
    try:
        ex.load_export(out)
    except ex.ExportError as e:
        return all(n in str(e) for n in needles), str(e)
    return False, "accepted"


def test_passed_export():
    """A5 + A6 + A7 + A10 + A15, and A8/A9 over the produced export."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup_rich(tmp)
        out = tmp / "export"
        before = _operator_state(repo)
        code, err = _run_fix(repo, _Doer(_rich_change), _export_args(tmp, out))
        _check("export: passed run exits 0", code == 0, f"code={code} err={err[-600:]}")
        _check("export: operator tree, index, HEAD and refs identical (A7)",
               _operator_state(repo) == before)
        ok, detail = _no_worktree_left(repo)
        _check("export: no worktree or .git/worktrees entry survives", ok, detail)
        try:
            manifest, mdigest = ex.load_export(out)
        except ex.ExportError as e:
            _check("export: produced export is acceptable", False, str(e))
            return
        names = set(manifest["payloads"])
        _check("export: payloads are the patch, station-run.json and exact inputs (A5)",
               names == {"cumulative.patch", "station-run.json", "spec.txt",
                         "acceptance.txt", "gate.sh"}, f"{names}")
        _check("export: manifest digest is the sha256 of its bytes",
               mdigest == hashlib.sha256((out / ex.MANIFEST_FILENAME).read_bytes()).hexdigest())
        art = sr.load_station_run_artifact(out)
        p = manifest["payloads"]
        _check("export: spec/acceptance/gate payloads resolve station-run's bindings",
               (p["spec.txt"], p["acceptance.txt"], p["gate.sh"])
               == (art.spec_digest, art.acceptance_digest, art.gate_digest))
        base = _git(repo, "rev-parse", "HEAD")
        _check("export: manifest names base, task, run, round, repo, digests (A6)",
               manifest["base_commit"] == base == art.base_commit
               and manifest["task_id"] == "task-42"
               and manifest["round"] == 3 and len(manifest["run_id"]) == 32
               and manifest["repository"]["name"] == "repo"
               and manifest["repository"]["root_commits"]
               == [_git(repo, "rev-list", "--max-parents=0", "HEAD")]
               and manifest["output_tree_digest"] == art.output_tree_digest
               and manifest["cumulative_patch_digest"] == p["cumulative.patch"]
               and manifest["station_artifact_digest"] == p["station-run.json"]
               and len(manifest["output_tree_oid"]) >= 40, json.dumps(manifest))
        _check("export: imported-result digest is an EXPLICIT absence (A6)",
               "imported_result_digest" in manifest
               and manifest["imported_result_digest"] is None)

        # A10: apply to a fresh checkout of the base; the tree digest must match.
        patch = (out / "cumulative.patch").read_bytes()
        _check("export: ignored path stays out of the patch",
               b"secret.env" not in patch and b"doer-ignored" not in patch)
        fresh = tmp / "fresh"
        subprocess.run(["git", "clone", "-q", str(repo), str(fresh)], check=True)
        applied = subprocess.run(["git", "apply", "--binary", "-"], cwd=fresh,
                                 input=patch, capture_output=True)
        _check("export: patch applies to a fresh checkout of the base",
               applied.returncode == 0, applied.stderr.decode())
        _check("export: patch carries deletion, exec bit, symlink, binary, addition",
               not (fresh / "gone.txt").exists()
               and os.access(fresh / "base.txt", os.X_OK)
               and (fresh / "link").is_symlink()
               and os.readlink(fresh / "link") == "feature.txt"
               and (fresh / "blob.bin").read_bytes() == bytes(range(256)) * 4
               and (fresh / "feature.txt").read_text() == "new\n")
        _check("export: reconstructed tree digest equals the manifest's (A10)",
               sr._output_tree_digest(fresh) == manifest["output_tree_digest"])

        # A8: the manifest is what makes the directory acceptable.
        raw = (out / ex.MANIFEST_FILENAME).read_bytes()
        (out / ex.MANIFEST_FILENAME).unlink()
        try:
            ex.load_export(out)
            _check("export: payloads without a manifest refused (A8)", False, "accepted")
        except ex.ExportError as e:
            _check("export: payloads without a manifest refused, naming it (A8)",
                   ex.MANIFEST_FILENAME in str(e) and "missing" in str(e), str(e))
        (out / ex.MANIFEST_FILENAME).write_bytes(raw)

        # A9: one flipped byte in a payload.
        target = out / "cumulative.patch"
        flipped = bytearray(patch)
        flipped[10] ^= 0x01
        target.write_bytes(bytes(flipped))
        try:
            ex.load_export(out)
            _check("export: flipped payload byte refused (A9)", False, "accepted")
        except ex.ExportError as e:
            msg = str(e)
            _check("export: flipped byte refused naming file and both digests (A9)",
                   "cumulative.patch" in msg and p["cumulative.patch"] in msg
                   and hashlib.sha256(bytes(flipped)).hexdigest() in msg, msg)
        target.write_bytes(patch)

        # Every input payload is required: missing from disk, or from the table.
        for name in ("spec.txt", "acceptance.txt", "gate.sh"):
            data = (out / name).read_bytes()
            (out / name).unlink()
            ok, msg = _refused(out, name)
            _check(f"export: missing {name} payload refused, naming it", ok, msg)
            (out / name).write_bytes(data)
            m = json.loads(raw)
            del m["payloads"][name]
            (out / ex.MANIFEST_FILENAME).write_text(json.dumps(m))
            (out / name).unlink()
            ok, msg = _refused(out, "manifest names no " + name)
            _check(f"export: {name} absent from dir and payload table refused", ok, msg)
            (out / name).write_bytes(data)
            (out / ex.MANIFEST_FILENAME).write_bytes(raw)
        m = json.loads(raw)
        del m["task_id"]
        (out / ex.MANIFEST_FILENAME).write_text(json.dumps(m))
        ok, msg = _refused(out, "task_id")
        _check("export: manifest without a task refused", ok, msg)
        (out / ex.MANIFEST_FILENAME).write_bytes(raw)
        _check("export: restored export is acceptable again", _refused(out)[1] == "accepted")


def test_unwritable_export_fails_the_run():
    """A11: a PASSED run whose export cannot be completed FAILS, in both formats."""
    for fmt in ("human", "json"):
        _unwritable_export(fmt)


def _unwritable_export(fmt: str):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        out = tmp / "export"
        out.mkdir()
        out.chmod(0o500)
        stdout = io.StringIO()
        log = tmp / "log.json"
        extra = [*_export_args(tmp, out),
                 *(["--json-output", "--log-file", str(log)] if fmt == "json" else [])]
        try:
            code, err = _run_fix(repo, _Doer(lambda: (Path("f.txt").write_text("x\n"), "done")[1]),
                                 extra, stdout=stdout)
        finally:
            out.chmod(0o700)
        text = stdout.getvalue()
        _check(f"unwritable[{fmt}]: passed run whose export fails exits 1 (A11)",
               code == 1 and "NOT exported" in err and str(out) in err,
               f"code={code} err={err[-600:]}")
        if fmt == "json":
            # stdout carries the run banner too; the log file holds the same object.
            try:
                res = json.loads(log.read_text())
            except (OSError, ValueError):
                res = {}
            _check("unwritable[json]: outcome is export_failed, station outcome kept (A11)",
                   res.get("outcome") == "export_failed"
                   and res.get("station_outcome") == "passed"
                   and res.get("export_status") == "failed"
                   and str(out) in (res.get("export_error") or "")
                   and "export_manifest_digest" not in res
                   and '"outcome": "export_failed"' in text, text[-600:])
        else:
            _check("unwritable[human]: summary says FAILED, not PASSED (A11)",
                   "FAILED (station passed, export failed)" in text
                   and "  PASSED  " not in text and "NOT exported" in text, text[-600:])
        _check("unwritable: nothing applied to the operator's tree",
               not (repo / "f.txt").exists())
        _check("unwritable: no manifest published",
               not (out / ex.MANIFEST_FILENAME).exists())
        ok, detail = _no_worktree_left(repo)
        _check("unwritable: no worktree survives", ok, detail)


def test_uncleanable_temp_name_is_an_export_failure():
    """codex r2 MINOR: `_write_atomic` raises ExportError when it cannot clear a temp
    NAME. `_export` caught only OSError/CheckpointError, so the exception escaped as a
    traceback instead of the promised export_failed result."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        out = tmp / "export"
        out.mkdir()
        (out / (ex.PATCH_FILENAME + ".tmp")).mkdir()   # unlink() cannot remove a dir
        stdout = io.StringIO()
        log = tmp / "log.json"
        code, err = _run_fix(
            repo, _Doer(lambda: (Path("f.txt").write_text("x\n"), "done")[1]),
            [*_export_args(tmp, out), "--json-output", "--log-file", str(log)],
            stdout=stdout)
        try:
            res = json.loads(log.read_text())
        except (OSError, ValueError):
            res = {}
        _check("uncleanable tmp: reported as export_failed, not a traceback",
               code == 1 and res.get("outcome") == "export_failed"
               and res.get("station_outcome") == "passed"
               and "Traceback" not in err,
               f"code={code} outcome={res.get('outcome')!r} err={err[-400:]}")
        _check("uncleanable tmp: the refusal names the temporary file",
               ex.PATCH_FILENAME + ".tmp" in (res.get("export_error") or ""),
               str(res.get("export_error"))[:300])
        _check("uncleanable tmp: nothing applied to the operator's tree",
               not (repo / "f.txt").exists())
        ok, detail = _no_worktree_left(repo)
        _check("uncleanable tmp: no worktree survives", ok, detail)


def test_failed_run_leaves_no_acceptable_export():
    """A12: a non-PASSED run writes nothing to the operator's tree and leaves no
    acceptable export -- a stale manifest from an earlier round is cleared."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        out = tmp / "export"
        out.mkdir()
        (out / ex.MANIFEST_FILENAME).write_text("{}")
        before = _operator_state(repo)

        def crash():
            Path("f.txt").write_text("x\n")
            raise RuntimeError("doer crashed")
        code, err = _run_fix(repo, _Doer(crash), _export_args(tmp, out))
        _check("failed: run exits 1", code == 1, f"code={code} err={err[-400:]}")
        _check("failed: operator tree untouched (A12)", _operator_state(repo) == before)
        try:
            ex.load_export(out)
            _check("failed: no acceptable export (A12)", False, "accepted")
        except ex.ExportError as e:
            _check("failed: no acceptable export, stale manifest cleared (A12)",
                   "missing" in str(e), str(e))
        ok, detail = _no_worktree_left(repo)
        _check("failed: no worktree survives", ok, detail)


def test_committed_head_is_ineligible_to_export():
    """A13: a worktree whose HEAD left the base cannot export; the reason is intact."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        out = tmp / "export"
        before = _operator_state(repo)

        def commit():
            Path("c.txt").write_text("c\n")
            subprocess.run(["git", "add", "c.txt"], check=True)
            subprocess.run(["git", "commit", "-qm", "doer"], check=True)
            Path("d.txt").write_text("d\n")  # a reviewable diff, so the run PASSES
            return "done"
        code, err = _run_fix(repo, _Doer(commit), _export_args(tmp, out))
        _check("committed: export refused, exit 1 (A13)",
               code == 1 and "the doer COMMITTED in the run's worktree (HEAD moved "
               "off the base)" in err, f"code={code} err={err[-600:]}")
        _check("committed: no manifest published",
               not (out / ex.MANIFEST_FILENAME).exists())
        _check("committed: operator tree untouched", _operator_state(repo) == before)
        ok, detail = _no_worktree_left(repo)
        _check("committed: no worktree survives", ok, detail)


def test_export_dir_refusals():
    """--export-dir needs --run-dir and must be outside the tree; refused up front."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup(tmp)
        doer = _Doer(lambda: "x")
        code, err = _run_fix(repo, doer, ["--export-dir", str(tmp / "e")])
        _check("refuse: --export-dir without --run-dir",
               code == 1 and "requires --run-dir" in err and not doer.cwds, err[-300:])
        code, err = _run_fix(repo, doer, ["--run-dir", str(tmp / "run"),
                                          "--export-dir", str(repo / "e"),
                                          "--export-task", "t"])
        _check("refuse: --export-dir inside the tree",
               code == 1 and "INSIDE the git working tree" in err and not doer.cwds,
               err[-300:])
        code, err = _run_fix(repo, doer, ["--run-dir", str(tmp / "run"),
                                          "--export-dir", str(tmp / "e")])
        _check("refuse: --export-dir without --export-task",
               code == 1 and "requires --export-task" in err and not doer.cwds,
               err[-300:])


def test_allow_dirty_and_v1_pinned():
    """A4 + A14: --allow-dirty's help text and three effects, and station-run v1."""
    import argparse
    p = argparse.ArgumentParser()
    cli.add_fix_parser(p.add_subparsers())
    fix = p._subparsers._group_actions[0].choices["fix"]
    help_ = next(a.help for a in fix._actions if "--allow-dirty" in a.option_strings)
    _check("allow-dirty: help text unchanged (A4)", help_ == (
        "Run against a dirty working tree; skips the clean-tree "
        "precondition AND all tree-restore behavior. The run's "
        "worktree is checked out at HEAD, so uncommitted changes "
        "are NOT visible to the run; on success its change is "
        "applied on top of them (refused if they overlap)"), help_)
    _check("allow-dirty: skips the clean-tree precondition (A4)",
           cli.ensure_clean_tree(True) is None)
    from harness.config import HarnessConfig
    from harness.orchestrator import _noop_restore_tree
    orch = cli.build_orchestrator(HarnessConfig(), allow_dirty=True)
    _check("allow-dirty: drops the restore (A4)",
           orch.restore_tree is _noop_restore_tree)
    _check("allow-dirty: logs tree_hygiene_disabled (A4)",
           any(e["event"] == "tree_hygiene_disabled" for e in orch.log), f"{orch.log}")
    fields = {k: v.description for k, v in sr.StationRunArtifact.model_fields.items()}
    _check("station-run: v1 field set and descriptions unchanged (A14)", fields == {
        "schema_version": None, "canon_version": None,
        "spec_digest": "sha256 of the spec text",
        "acceptance_digest": "sha256 of the acceptance text",
        "gate_digest": "sha256 of the gate bytes, captured at run start",
        "base_commit": "repo HEAD the run started from",
        "output_tree_digest": "ls-tree digest of the add-A output tree",
        "terminal_state": "station-attested FeatureResult outcome",
    } and sr.STATION_RUN_SCHEMA_VERSION == 1, f"{fields}")


# --- codex r1 folds ---------------------------------------------------------- #

def _valid_export(out: Path, *, base="a" * 40, tree_oid="b" * 40,
                  tree_digest="treedigest", patch=b"PATCH\n") -> dict:
    """A well-formed export built WITHOUT a run: the station artifact's input digests
    are computed exactly as `write_export` encodes each payload, so the cross-file
    checks in `load_export` are satisfied and each negative below isolates one field."""
    from harness.checkpoint import digest_text
    spec, acceptance, gate = "the spec", "the acceptance", b"#!/bin/sh\nexit 0\n"
    art = {"schema_version": 1, "canon_version": sr.STATION_RUN_CANON,
           "spec_digest": digest_text(spec), "acceptance_digest": digest_text(acceptance),
           "gate_digest": sr.gate_digest_of_bytes(gate), "base_commit": base,
           "output_tree_digest": tree_digest, "terminal_state": "passed"}
    art_bytes = (json.dumps(art, indent=1, sort_keys=True) + "\n").encode()
    ex.write_export(out, task_id="t", run_id="r" * 32, round_index=0,
                    repository={"name": "repo", "push_url_raw": None, "root_commits": []},
                    base_commit=base, output_tree_oid=tree_oid,
                    output_tree_digest=tree_digest, patch=patch,
                    station_run_bytes=art_bytes, spec=spec, acceptance=acceptance,
                    gate_bytes=gate)
    return json.loads((out / ex.MANIFEST_FILENAME).read_bytes())


def _rewrite_manifest(out: Path, manifest) -> None:
    (out / ex.MANIFEST_FILENAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def test_tmp_symlink_cannot_write_through():
    """codex r1 MAJOR: a pre-existing `<payload>.tmp` symlink pointing into the
    operator's checkout must not be written THROUGH. Path.write_bytes followed it, so
    the export truncated a tracked operator file from the one path whose purpose is to
    touch nothing outside the export directory -- and still reported success."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo = _setup_rich(tmp)
        out = tmp / "export"
        out.mkdir()
        victim = repo / "base.txt"
        planted = victim.read_bytes()
        os.symlink(victim, out / (ex.PATCH_FILENAME + ".tmp"))
        before = _operator_state(repo)
        code, err = _run_fix(repo, _Doer(_rich_change), _export_args(tmp, out))
        _check("symlink: the operator's file was NOT written through",
               victim.read_bytes() == planted,
               f"{victim} changed: {victim.read_bytes()[:80]!r}")
        _check("symlink: operator tree, index, HEAD and refs still identical",
               _operator_state(repo) == before)
        _check("symlink: the payload is a regular file, not a link",
               (out / ex.PATCH_FILENAME).is_file()
               and not (out / ex.PATCH_FILENAME).is_symlink())
        _check("symlink: the export still completed", code == 0,
               f"code={code} err={err[-400:]}")


def test_reuse_and_interrupted_publication():
    """codex r1 MINOR: prove publication safety over a GENUINE earlier export and a
    failure injected after some payloads were replaced, not only over a deleted
    manifest and an unwritable directory."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        out = tmp / "export"
        first = _valid_export(out, patch=b"ROUND0\n")
        _check("reuse: the earlier export is genuinely acceptable",
               ex.load_export(out)[0]["cumulative_patch_digest"]
               == first["cumulative_patch_digest"])

        real = ex._write_atomic
        for label, failing in (("a payload", ex.PATCH_FILENAME),
                               ("the manifest", ex.MANIFEST_FILENAME)):
            _valid_export(out, patch=b"ROUND0\n")           # a complete earlier export
            ex.clear_export(out)                            # what the CLI does pre-run
            try:
                def boom(path: Path, data: bytes, _real=real, _f=failing):
                    if path.name == _f:
                        raise OSError(f"injected failure writing {_f}")
                    _real(path, data)
                ex._write_atomic = boom
                try:
                    _valid_export(out, patch=b"ROUND1\n")
                    wrote = True
                except OSError:
                    wrote = False
            finally:
                ex._write_atomic = real
            _check(f"reuse: a failure writing {label} aborts the export", not wrote)
            ok, detail = _refused(out, ex.MANIFEST_FILENAME, "missing")
            _check(f"reuse: after a failure writing {label}, the earlier export is "
                   f"NOT offered as this run's result", ok, detail)


def test_loader_refuses_unverified_manifests():
    """codex r1 MAJOR: the loader accepted a manifest with no run/round/repository/base
    /output identity whose payloads were arbitrary bytes, accepted `schema_version:
    true` because `True == 1`, and raised AttributeError rather than ExportError on a
    top-level JSON array."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td).resolve() / "export"
        for field in ("run_id", "round", "repository", "base_commit",
                      "output_tree_oid", "output_tree_digest",
                      "imported_result_digest"):
            m = _valid_export(out)
            del m[field]
            _rewrite_manifest(out, m)
            ok, detail = _refused(out, field)
            _check(f"loader: a manifest with no {field} is refused by name", ok, detail)

        m = _valid_export(out)
        m["schema_version"] = True
        _rewrite_manifest(out, m)
        ok, detail = _refused(out, "boolean")
        _check("loader: schema_version: true is refused (True == 1 in Python)", ok, detail)

        _valid_export(out)
        _rewrite_manifest(out, ["not", "an", "object"])
        ok, detail = _refused(out, "not a JSON object")
        _check("loader: a top-level array is a named ExportError, not AttributeError",
               ok, detail)

        m = _valid_export(out)
        m["base_commit"] = "c" * 39
        _rewrite_manifest(out, m)
        ok, detail = _refused(out, "40-char")
        _check("loader: a base_commit that is not a 40-char hex OID is refused", ok, detail)

        # The station artifact must AGREE with the manifest it ships beside.
        m = _valid_export(out, base="d" * 40)
        m["base_commit"] = "e" * 40
        _rewrite_manifest(out, m)
        ok, detail = _refused(out, "station-run.json", "base_commit")
        _check("loader: a station artifact disagreeing on base_commit is refused",
               ok, detail)

        m = _valid_export(out, tree_digest="one")
        m["output_tree_digest"] = "another"
        _rewrite_manifest(out, m)
        ok, detail = _refused(out, "station-run.json", "output_tree_digest")
        _check("loader: a station artifact disagreeing on output_tree_digest is refused",
               ok, detail)

        m = _valid_export(out)
        (out / ex.SPEC_FILENAME).write_bytes(b"a different spec")
        m["payloads"][ex.SPEC_FILENAME] = hashlib.sha256(b"a different spec").hexdigest()
        _rewrite_manifest(out, m)
        ok, detail = _refused(out, "spec_digest")
        _check("loader: inputs that no longer resolve the artifact's bindings are "
               "refused", ok, detail)


def main() -> bool:
    for t in (test_passed_export, test_unwritable_export_fails_the_run,
              test_failed_run_leaves_no_acceptable_export,
              test_committed_head_is_ineligible_to_export,
              test_export_dir_refusals, test_allow_dirty_and_v1_pinned,
              test_tmp_symlink_cannot_write_through,
              test_reuse_and_interrupted_publication,
              test_loader_refuses_unverified_manifests,
              test_uncleanable_temp_name_is_an_export_failure):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
