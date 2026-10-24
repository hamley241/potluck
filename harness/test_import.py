"""Tests for the station IMPORT of an attested result (ruling 001, part 2 of 3).

Drives the REAL `cmd_fix` path against a real temp git repo with only the doer
swapped (the `test_run_worktree` harness). Round 0 is a genuine part-1 export; every
negative is a constructed fixture that must be refused, NAMING why, before any doer
call:

  * A5/A6: an importing run's doer starts from a tree whose digest equals the imported
    output_tree_digest, in a worktree whose HEAD is still the operator's HEAD.
  * A7: one flipped byte of cumulative.patch; A8: a manifest and artifact that agree
    but attest a tree the patch does not produce; A9: a patch that does not apply;
    A10: payloads with no manifest.
  * A11: the operator's tree and the export directory are byte-identical after, and
    a --run-dir or --export-dir overlapping the import directory is refused by name.
  * Ruling 001 base: an import onto a HEAD other than the manifest's base_commit (an
    empty commit included) is refused, naming both commits.
  * A12/A13: a two-round chain walks back by manifest digest; a non-import is null.
  * A14: a retry restores the imported state; --resume of an importing run is
    REFUSED by name (the chosen half -- see cli._cmd_fix).
  * A15: the restore stays wired for an importing run (source and object level).
  * A16/A17/A18: base_commit stays HEAD, output digest is the full tree, a doer commit
    is still refused, and no worktree survives any path.

Runnable: `python3 -m harness.test_import`.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
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


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _dir_state(d: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.iterdir()) if p.is_file()}


class _Seeing(_Doer):
    """Records, at the doer's start, the worktree's tree digest and HEAD."""
    def __init__(self, implement):
        super().__init__(implement)
        self.seen: list[tuple[str, str]] = []

    async def implement(self, spec, acceptance):
        self.seen.append((sr._output_tree_digest(Path.cwd()),
                          _git(Path.cwd(), "rev-parse", "HEAD")))
        return await super().implement(spec, acceptance)


def _exporting(tmp: Path) -> list[str]:
    """Part 3 refuses --import-dir without --export-dir, so every importing run here
    also exports (to a directory disjoint from the import)."""
    return ["--run-dir", str(tmp / "run1"), "--export-dir", str(tmp / "export1"),
            "--export-task", "t"]


def _round0(tmp: Path) -> tuple[Path, Path]:
    repo = _setup_rich(tmp)
    # An origin, so the export records push_url_raw and its result is deliverable.
    _git(repo, "remote", "add", "origin", "https://example.invalid/o/repo.git")
    out0 = tmp / "export0"
    code, err = _run_fix(repo, _Doer(_rich_change), _export_args(tmp, out0))
    assert code == 0, err[-600:]
    return repo, out0


def _reexport(src: Path, dst: Path, *, patch: bytes | None = None,
              tree_digest: str | None = None) -> None:
    """Rewrite a genuine export with a different patch and/or attested tree digest,
    keeping manifest, artifact and payload table mutually CONSISTENT -- so load_export
    accepts it and only the import's own reconstruction can refuse it."""
    m, _ = ex.load_export(src)
    art = json.loads((src / sr.STATION_RUN_FILENAME).read_bytes())
    if tree_digest is not None:
        art["output_tree_digest"] = tree_digest
    ex.write_export(
        dst, task_id=m["task_id"], run_id=m["run_id"], round_index=m["round"],
        repository=m["repository"], base_commit=m["base_commit"],
        output_tree_oid=m["output_tree_oid"],
        output_tree_digest=art["output_tree_digest"],
        patch=(src / ex.PATCH_FILENAME).read_bytes() if patch is None else patch,
        station_run_bytes=(json.dumps(art, indent=1, sort_keys=True) + "\n").encode(),
        spec=(src / ex.SPEC_FILENAME).read_text(),
        acceptance=(src / ex.ACCEPTANCE_FILENAME).read_text(),
        gate_bytes=(src / ex.GATE_FILENAME).read_bytes())


def test_import_and_chain():
    """A5 A6 A11 A12 A13 A16 A18 over a genuine two-round chain."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        m0, d0 = ex.load_export(out0)
        _check("chain: a non-importing export records imported_result_digest null (A13)",
               m0["imported_result_digest"] is None)
        before, export_before = _operator_state(repo), _dir_state(out0)
        head = _git(repo, "rev-parse", "HEAD")

        def more():
            Path("round1.txt").write_text("round 1\n")
            return "done"
        doer = _Seeing(more)
        out1 = tmp / "export1"
        code, err = _run_fix(repo, doer, ["--import-dir", str(out0),
                                          "--run-dir", str(tmp / "run1"),
                                          "--export-dir", str(out1),
                                          "--export-task", "task-42",
                                          "--export-round", "1"])
        _check("import: the importing run passes", code == 0,
               f"code={code} err={err[-600:]}")
        _check("import: the doer starts from the attested output tree digest (A5)",
               doer.seen and doer.seen[0][0] == m0["output_tree_digest"], f"{doer.seen}")
        _check("import: worktree HEAD is still the operator's committed HEAD (A6)",
               doer.seen and doer.seen[0][1] == head, f"{doer.seen} head={head}")
        _check("import: operator tree, index, HEAD and refs identical (A11)",
               _operator_state(repo) == before)
        _check("import: the export directory is byte-identical (A11)",
               _dir_state(out0) == export_before)
        ok, detail = _no_worktree_left(repo)
        _check("import: no worktree survives (A18)", ok, detail)

        m1, d1 = ex.load_export(out1)
        _check("chain: round 1 records round 0's MANIFEST digest (A12)",
               m1["imported_result_digest"] == d0 != m0["cumulative_patch_digest"],
               f"{m1['imported_result_digest']} vs {d0}")
        walked, cur = [], out1
        by_digest = {d0: out0, d1: out1}
        while cur is not None:
            m, d = ex.load_export(cur)
            walked.append(d)
            cur = by_digest.get(m["imported_result_digest"])
        _check("chain: walking from round 1 names round 0, then ends (A12)",
               walked == [d1, d0], f"{walked}")
        art1 = sr.load_station_run_artifact(out1)
        _check("v1: base_commit is the run's starting HEAD, a real commit (A16)",
               art1.base_commit == m1["base_commit"] == head
               and _git(repo, "cat-file", "-t", head) == "commit")
        fresh = tmp / "fresh"
        subprocess.run(["git", "clone", "-q", str(repo), str(fresh)], check=True)
        subprocess.run(["git", "apply", "--binary", "-"], cwd=fresh, check=True,
                       input=(out1 / ex.PATCH_FILENAME).read_bytes())
        _check("v1: output_tree_digest is the FULL tree, both rounds' work (A16)",
               sr._output_tree_digest(fresh) == art1.output_tree_digest
               and (fresh / "feature.txt").exists() and (fresh / "round1.txt").exists())
        _check("v1: no synthetic baseline commit was created (A16)",
               _git(repo, "rev-list", "--all", "--count") == "2")


def _refusal_case(name: str, mutate, *needles: str):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        bad = tmp / "bad"
        mutate(out0, bad)
        before, bad_before = _operator_state(repo), _dir_state(bad)
        doer = _Doer(lambda: "x")
        code, err = _run_fix(repo, doer, ["--import-dir", str(bad), *_exporting(tmp)])
        missing = [n(out0) if callable(n) else n for n in needles]
        missing = [n for n in missing if n not in err]
        _check(f"{name}: refused, naming the reason", code == 1 and not missing,
               f"code={code} missing={missing} err={err[-600:]}")
        _check(f"{name}: the doer was NOT called", not doer.cwds, f"{doer.cwds}")
        _check(f"{name}: operator tree untouched", _operator_state(repo) == before)
        _check(f"{name}: import directory untouched", _dir_state(bad) == bad_before)
        ok, detail = _no_worktree_left(repo)
        _check(f"{name}: no worktree survives", ok, detail)


def _flip(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)
    data = bytearray((dst / ex.PATCH_FILENAME).read_bytes())
    data[10] ^= 0x01
    (dst / ex.PATCH_FILENAME).write_bytes(bytes(data))


def _no_manifest(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)
    (dst / ex.MANIFEST_FILENAME).unlink()


_FAKE = "f" * 64
_NOAPPLY = (b"diff --git a/base.txt b/base.txt\n--- a/base.txt\n+++ b/base.txt\n"
            b"@@ -1 +1 @@\n-not what the base holds\n+x\n")


def test_refusals():
    """A7 A8 A9 A10: each refused before any doer call, naming why."""
    _refusal_case("corrupt patch (A7)", _flip, "import refused", ex.PATCH_FILENAME,
                  "does not match its manifest digest")
    _refusal_case("wrong attested tree (A8)",
                  lambda s, d: _reexport(s, d, tree_digest=_FAKE),
                  "import refused", _FAKE,
                  lambda out0: ex.load_export(out0)[0]["output_tree_digest"])
    _refusal_case("patch does not apply (A9)",
                  lambda s, d: _reexport(s, d, patch=_NOAPPLY),
                  "import refused", "does not apply", "base.txt")
    _refusal_case("no manifest (A10)", _no_manifest, "import refused",
                  ex.MANIFEST_FILENAME, "the export never completed")


class _HangOnce:
    """First attempt mangles the imported state then hangs past the doer timeout;
    the retry records what it starts from."""
    def __init__(self):
        self.cwds: list[str] = []
        self.seen: list[str] = []

    async def implement(self, spec, acceptance):
        self.cwds.append(os.getcwd())
        self.seen.append(sr._output_tree_digest(Path.cwd()))
        if len(self.cwds) == 1:
            Path("feature.txt").unlink()
            Path("junk.txt").write_text("dead attempt\n")
            await asyncio.sleep(30)
        Path("round1.txt").write_text("r1\n")
        return "done"


def test_retry_restores_imported_state():
    """A14 (retry half) + A15: the retry starts from the imported state, not HEAD."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        want = ex.load_export(out0)[0]["output_tree_digest"]
        doer = _HangOnce()
        # AMENDED IN PART 3 (deliver-by-digest spec, design point 3), deliberately: this
        # test used to run `--import-dir` alone and assert the run DELIVERED both rounds'
        # work directly. Part 3 refuses an importing run without --export-dir -- direct
        # delivery of an imported candidate was a third delivery path with none of the
        # ruling's required safeguards -- so the run now exports, and the result is
        # delivered explicitly by its manifest digest. The retry claim is unchanged.
        # AMENDED (ruling 002 R3, the lease): potluck now makes ONE attempt per
        # bounded call, so there is no in-call retry by default. The restore-before-
        # each-attempt wiring this test proves still runs whenever a retry policy
        # allows one, so the run opts back into retries_before_counting = 1.
        real_build = cli.build_orchestrator

        def build_with_retry(cfg, **kw):
            orch = real_build(cfg, **kw)
            orch.cfg.escalation.retries_before_counting = 1
            return orch
        cli.build_orchestrator = build_with_retry
        try:
            code, err = _run_fix(repo, doer, ["--import-dir", str(out0),
                                              "--doer-timeout", "1", *_exporting(tmp)])
        finally:
            cli.build_orchestrator = real_build
        _check("retry: the run passes after a timed-out attempt", code == 0,
               f"code={code} err={err[-600:]}")
        _check("retry: both attempts start from the imported tree digest (A14)",
               len(doer.seen) == 2 and doer.seen == [want, want], f"{doer.seen}")
        _check("retry: the importing run delivered NOTHING itself (part 3)",
               not (repo / "round1.txt").exists())
        from harness.test_deliver import _deliver
        code, err, _ = _deliver(repo, tmp / "export1",
                                ex.load_export(tmp / "export1")[1])
        _check("retry: delivery carries both rounds' work, not the dead attempt",
               code == 0 and (repo / "feature.txt").exists()
               and (repo / "round1.txt").exists() and not (repo / "junk.txt").exists(),
               f"code={code} err={err[-600:]}")
        ok, detail = _no_worktree_left(repo)
        _check("retry: no worktree survives", ok, detail)

    from harness.config import HarnessConfig
    from harness.orchestrator import real_restore_tree

    async def mine():
        pass
    _check("wired: an importing run's orchestrator restores via the import (A15)",
           cli.build_orchestrator(HarnessConfig(), import_restore=mine).restore_tree
           is mine)
    _check("wired: a non-importing run keeps the baseline restore (A3)",
           cli.build_orchestrator(HarnessConfig()).restore_tree is real_restore_tree)
    src = inspect.getsource(cli._cmd_fix)
    _check("wired: the fresh path passes the import restore to the orchestrator (A15)",
           "import_restore=import_restore" in src
           and "run.import_restore(import_patch, import_tree_digest," in src)
    src = inspect.getsource(cli._RunWorktree.import_restore)
    _check("wired: the import restore resets AND re-verifies the import (A15)",
           '"reset", "--hard"' in src and "self.apply_import(" in src)


def test_resume_refused_by_name():
    """A14 (resume half): the chosen behaviour is a named refusal."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        run = tmp / "run1"

        def crash():
            raise RuntimeError("doer crashed")
        code, _ = _run_fix(repo, _Doer(crash), ["--import-dir", str(out0),
                                                *_exporting(tmp)])
        _check("resume: the crashed importing run marks its run-dir",
               code == 1 and (run / cli._IMPORT_MARKER_NAME).exists())
        for label, extra in (("without --import-dir", ["--resume", str(run)]),
                             ("with --import-dir", ["--resume", str(run),
                                                    "--import-dir", str(out0)])):
            doer = _Doer(lambda: "x")
            code, err = _run_fix(repo, doer, extra)
            _check(f"resume: an importing resume {label} is refused by name (A14)",
                   code == 1 and "--resume of an importing run is not supported" in err
                   and "not built" in err and not doer.cwds, err[-400:])
            ok, detail = _no_worktree_left(repo)
            _check(f"resume: no worktree survives the refusal {label} (A18)", ok, detail)
        code, err = _run_fix(repo, _Doer(lambda: "x"),
                             ["--import-dir", str(out0), "--run-dir", str(run),
                              "--export-dir", str(out0), "--export-task", "t"])
        _check("import: --export-dir equal to --import-dir is refused",
               code == 1 and "--export-dir overlap" in err
               and ex.load_export(out0)[0] is not None, err[-300:])


def test_run_dir_overlapping_import_refused():
    """A11: a --run-dir that is (or sits inside) the import directory is refused by
    name before any write, so the accepted export's bytes cannot be touched."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        for label, rd in (("equal to", out0), ("inside", out0 / "sub")):
            before, export_before = _operator_state(repo), _dir_state(out0)
            doer = _Doer(lambda: "x")
            code, err = _run_fix(repo, doer, ["--import-dir", str(out0),
                                              "--run-dir", str(rd),
                                              "--export-dir", str(tmp / "e"),
                                              "--export-task", "t"])
            _check(f"overlap: --run-dir {label} --import-dir is refused by name (A11)",
                   code == 1 and "--import-dir and --run-dir overlap" in err,
                   f"code={code} err={err[-400:]}")
            _check(f"overlap: the doer was NOT called ({label})", not doer.cwds,
                   f"{doer.cwds}")
            _check(f"overlap: the import directory is byte-identical ({label})",
                   _dir_state(out0) == export_before and not (out0 / "sub").exists()
                   and ex.load_export(out0)[0] is not None)
            _check(f"overlap: operator tree untouched ({label})",
                   _operator_state(repo) == before)
            ok, detail = _no_worktree_left(repo)
            _check(f"overlap: no worktree survives ({label})", ok, detail)


def test_patch_substituted_after_acceptance():
    """codex r1 MAJOR: load_export verifies the payloads, then _load_import READ the
    patch again -- so a substitution between the two reads was applied unverified. The
    tree-digest check does not close it: a substituted patch can produce the attested
    tree PLUS an ignored file, which the digest excludes by design and the doer and gate
    can still consume. import_restore would reapply those bytes on every retry."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        real_load = ex.load_export
        swapped = {}

        def load_then_swap(d):
            out = real_load(d)
            patch = Path(d) / ex.PATCH_FILENAME
            swapped["before"] = hashlib.sha256(patch.read_bytes()).hexdigest()
            # Accepted, then replaced: the attested tree plus an ignored path.
            patch.write_bytes(patch.read_bytes() + b"\n")
            swapped["after"] = hashlib.sha256(patch.read_bytes()).hexdigest()
            return out

        doer = _Doer(lambda: (Path("round1.txt").write_text("r1\n"), "done")[1])
        try:
            ex.load_export = load_then_swap
            code, err = _run_fix(repo, doer, ["--import-dir", str(out0),
                                              "--run-dir", str(tmp / "run1"),
                                              "--export-dir", str(tmp / "export1"),
                                              "--export-task", "t"])
        finally:
            ex.load_export = real_load
        _check("substitution: the bytes actually applied are re-verified",
               code == 1 and "changed after the export was accepted" in err
               and swapped.get("after", "") in err,
               f"code={code} err={err[-400:]}")
        _check("substitution: the doer never ran", not doer.cwds, f"{doer.cwds}")
        _check("substitution: nothing reached the operator's tree",
               not (repo / "round1.txt").exists())


def test_link_into_import_refused():
    """codex r1 MAJOR: `_overlaps` compares PATHS, so disjoint directories passed it --
    but a pre-created `run-dir/imported-result.json -> <import>/station-run.json` was
    followed by the marker write, truncating the accepted input artifact before the doer
    ran."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        before = _dir_state(out0)
        run1 = tmp / "run1"
        run1.mkdir()
        os.symlink(out0 / sr.STATION_RUN_FILENAME, run1 / cli._IMPORT_MARKER_NAME)
        doer = _Doer(lambda: (Path("round1.txt").write_text("r1\n"), "done")[1])
        code, err = _run_fix(repo, doer, ["--import-dir", str(out0),
                                          "--run-dir", str(run1),
                                          "--export-dir", str(tmp / "export1"),
                                          "--export-task", "t"])
        _check("link: a symlink from the run-dir into the import is refused by name",
               code == 1 and "reaches the import" in err,
               f"code={code} err={err[-400:]}")
        _check("link: the accepted export's bytes are untouched",
               _dir_state(out0) == before)
        try:
            still_ok, why = ex.load_export(out0)[1] is not None, ""
        except ex.ExportError as e:
            # Must REPORT, not raise: without the fold the marker write truncates the
            # accepted station-run.json and this assertion would abort the whole suite
            # instead of recording the failure it exists to catch.
            still_ok, why = False, str(e)
        _check("link: the import stays acceptable afterwards", still_ok, why)
        _check("link: the doer never ran", not doer.cwds, f"{doer.cwds}")


def test_moved_head_refused():
    """Ruling 001: every round starts at the original base. An empty commit leaves the
    tree digest unchanged, so only the base check can refuse it -- before the doer."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        base0 = ex.load_export(out0)[0]["base_commit"]
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "empty"],
                       cwd=repo, check=True)
        head = _git(repo, "rev-parse", "HEAD")
        before, export_before = _operator_state(repo), _dir_state(out0)
        doer = _Doer(lambda: "x")
        code, err = _run_fix(repo, doer, ["--import-dir", str(out0), *_exporting(tmp)])
        _check("base: an import onto a moved HEAD is refused, naming both commits",
               code == 1 and "import refused" in err and head in err and base0 in err
               and "base_commit" in err, f"code={code} err={err[-600:]}")
        _check("base: the doer was NOT called", not doer.cwds, f"{doer.cwds}")
        _check("base: operator tree untouched", _operator_state(repo) == before)
        _check("base: import directory untouched", _dir_state(out0) == export_before)
        ok, detail = _no_worktree_left(repo)
        _check("base: no worktree survives", ok, detail)


def test_doer_commit_still_refused():
    """A17: head != base stays a hard refusal for an importing run, reason intact."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td).resolve()
        repo, out0 = _round0(tmp)
        before = _operator_state(repo)

        def commit():
            Path("c.txt").write_text("c\n")
            subprocess.run(["git", "add", "-A"], check=True)
            subprocess.run(["git", "commit", "-qm", "doer"], check=True)
            Path("d.txt").write_text("d\n")
            return "done"
        code, err = _run_fix(repo, _Doer(commit), ["--import-dir", str(out0),
                                                   *_exporting(tmp)])
        _check("commit: an importing run whose doer commits is refused (A17)",
               code == 1 and "the doer COMMITTED in the run's worktree (HEAD moved "
               "off the base)" in err, f"code={code} err={err[-600:]}")
        _check("commit: operator tree untouched", _operator_state(repo) == before)
        ok, detail = _no_worktree_left(repo)
        _check("commit: no worktree survives (A18)", ok, detail)


def main() -> bool:
    for t in (test_import_and_chain, test_refusals, test_retry_restores_imported_state,
              test_resume_refused_by_name, test_run_dir_overlapping_import_refused,
              test_patch_substituted_after_acceptance,
              test_link_into_import_refused,
              test_moved_head_refused, test_doer_commit_still_refused):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
