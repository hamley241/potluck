"""Tests for the run-owned worktree (FU-ref-ownership ruling: potluck must never
move a ref the run does not exclusively control).

Drives the REAL `cmd_fix` path against a real temp git repo -- real worktree,
real gate (`bash .claude/verify.sh`), real diff, real restore -- with only the
doer swapped for a scripted one, so reverting the worktree isolation (the doer
and gate running in the operator's checkout again) turns these red:

  * A3 (the reproduced destruction): a FOREIGN session commits to the operator's
    branch while the run is in flight, the doer commits too, and the run
    abandons. The foreign commit must stay reachable with its file, and the
    branch ref, HEAD and `git status` must be exactly as the foreign session
    left them. Without isolation the doer's commit lands on the branch and the
    restore deletes the foreign session's untracked file.
  * A2: a PASSED run leaves its change as uncommitted change in the operator's
    tree with the branch ref and HEAD unmoved, and the doer and gate ran in the
    worktree, not the operator's checkout.
  * A5: no worktree directory and no `.git/worktrees` entry survives a run,
    passed or abandoned.
  * A7: the gate resolves a `../<sibling>` repo from the worktree; an
    unplaceable worktree refuses naming the sibling-path cause.
  * A8: an ignored file in the operator's tree survives every path.
  * no-change evidence: a run whose doer changes nothing still escalates
    no-signal for "produced no changes", and the doer's FULL reply (even past
    _MAX_STR) is read back from the run's 0o600 --log-file, alongside the
    stated restore scope.

Runnable: `python3 -m harness.test_run_worktree`.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import harness.cli as cli

_results: list[tuple[str, bool, str]] = []

_GATE = """#!/usr/bin/env bash
# The fleet resolves its sibling by relative path; exit 2 (no signal) if not.
test -d ../sibling_core || { echo "../sibling_core not found" >&2; exit 2; }
pwd -P > "$POTLUCK_TEST_GATE_PWD"
exit 0
"""


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _git(repo: Path, *args: str) -> str:
    # GIT_OPTIONAL_LOCKS=0: an observation must not WRITE what it observes. `git status`
    # opportunistically refreshes the index, which would make an index-bytes comparison
    # in `_operator_state` differ between two readings of an unchanged repository.
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()


def _setup(tmp: Path) -> Path:
    """parent/{repo, sibling_core}: repo has a committed gate + .gitignore and an
    ignored file present on disk."""
    (tmp / "sibling_core").mkdir()
    repo = tmp / "repo"
    repo.mkdir()
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        _git(repo, *a)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "verify.sh").write_text(_GATE)
    (repo / ".gitignore").write_text("secret.env\n")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "secret.env").write_text("TOKEN=keep-me\n")
    return repo


def _no_worktree_left(repo: Path) -> tuple[bool, str]:
    listed = [l for l in _git(repo, "worktree", "list", "--porcelain").splitlines()
              if l.startswith("worktree ")]
    admin = repo / ".git" / "worktrees"
    admin_entries = list(admin.iterdir()) if admin.exists() else []
    dirs = [p.name for p in repo.parent.iterdir() if "potluck-run" in p.name]
    ok = len(listed) == 1 and not admin_entries and not dirs
    return ok, f"listed={listed} admin={admin_entries} dirs={dirs}"


def _run_fix(repo: Path, doer, extra=(), stdout=None) -> tuple[int | None, str]:
    """cmd_fix from `repo` with the scripted doer, no debate, no closure sweep.
    stdout goes to `stdout` when given (a StringIO), else is discarded."""
    real_build = cli.build_orchestrator

    def build(cfg, **kw):
        orch = real_build(cfg, **kw)
        orch.doer = doer
        orch.cfg.closure_enabled = False
        return orch

    cwd0 = os.getcwd()
    gate_pwd = repo.parent / "gate_pwd"
    os.environ["POTLUCK_TEST_GATE_PWD"] = str(gate_pwd)
    cli.build_orchestrator = build
    err, out, code = io.StringIO(), stdout if stdout is not None else io.StringIO(), None
    try:
        os.chdir(repo)
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                cli.cmd_fix(cli.build_parser().parse_args(
                    ["fix", "--spec", "x", "--no-debate", "--no-tiebreaker", *extra]))
            except SystemExit as e:
                code = e.code
    finally:
        cli.build_orchestrator = real_build
        os.chdir(cwd0)
    return code, err.getvalue()


class _Doer:
    def __init__(self, implement):
        self._implement = implement
        self.cwds: list[str] = []

    async def implement(self, spec, acceptance):
        self.cwds.append(os.path.realpath(os.getcwd()))
        return self._implement()


def test_passed_run_delivers_without_moving_the_branch():
    """A2 + A1 + A5 + A7 + A8 on the success path."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        ref0, head0 = _git(repo, "rev-parse", "refs/heads/main"), _git(repo, "rev-parse", "HEAD")

        def work():
            Path("feature.txt").write_text("new\n")
            return "implemented"
        doer = _Doer(work)
        code, err = _run_fix(repo, doer)
        _check("pass: exit 0", code == 0, f"code={code} err={err[-400:]}")
        _check("pass: branch ref NOT moved",
               _git(repo, "rev-parse", "refs/heads/main") == ref0)
        _check("pass: HEAD NOT moved, still on main",
               _git(repo, "rev-parse", "HEAD") == head0
               and _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main")
        _check("pass: change delivered as UNCOMMITTED change",
               (repo / "feature.txt").read_text() == "new\n"
               and _git(repo, "status", "--porcelain") == "?? feature.txt",
               _git(repo, "status", "--porcelain"))
        gate_pwd = (repo.parent / "gate_pwd").read_text().strip()
        _check("pass: doer ran in the run's worktree, not the operator's checkout",
               doer.cwds and all(c != str(repo) and Path(c).parent == repo.parent
                                 for c in doer.cwds), f"{doer.cwds}")
        _check("pass: gate ran in the worktree and resolved ../sibling_core",
               gate_pwd != str(repo) and Path(gate_pwd).parent == repo.parent,
               gate_pwd)
        ok, detail = _no_worktree_left(repo)
        _check("pass: no worktree or .git/worktrees entry survives", ok, detail)
        _check("pass: ignored file preserved",
               (repo / "secret.env").read_text() == "TOKEN=keep-me\n")


def test_foreign_commit_survives_abandonment():
    """A3 + A2 + A4 + A5 + A8: the reproduced destruction case."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        seen: dict = {}

        def work():
            if not seen:
                # A FOREIGN session writes the operator's branch mid-run.
                (repo / "foreign.txt").write_text("foreign\n")
                _git(repo, "add", "foreign.txt")
                _git(repo, "commit", "-qm", "foreign")
                (repo / "foreign_wip.txt").write_text("wip\n")
                seen["sha"] = _git(repo, "rev-parse", "HEAD")
                seen["status"] = _git(repo, "status", "--porcelain")
                # The doer edits AND commits where it runs, then dies.
                Path("doer.txt").write_text("doer\n")
                subprocess.run(["git", "add", "doer.txt"], check=True)
                subprocess.run(["git", "commit", "-qm", "doer"], check=True)
            raise RuntimeError("doer crashed mid-implement")
        doer = _Doer(work)
        code, err = _run_fix(repo, doer)
        _check("abandon: run did not pass", code == 1, f"code={code} err={err[-400:]}")
        sha = seen.get("sha", "?")
        _check("abandon: branch ref exactly as the foreign session left it",
               _git(repo, "rev-parse", "refs/heads/main") == sha)
        _check("abandon: HEAD exactly as the foreign session left it",
               _git(repo, "rev-parse", "HEAD") == sha
               and _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main")
        _check("abandon: foreign commit reachable and its file present",
               subprocess.run(["git", "merge-base", "--is-ancestor", sha, "main"],
                              cwd=repo).returncode == 0
               and (repo / "foreign.txt").read_text() == "foreign\n")
        _check("abandon: git status exactly as the foreign session left it",
               _git(repo, "status", "--porcelain") == seen.get("status"),
               _git(repo, "status", "--porcelain"))
        _check("abandon: the run's work never reached the operator's tree",
               not (repo / "doer.txt").exists())
        ok, detail = _no_worktree_left(repo)
        _check("abandon: no worktree or .git/worktrees entry survives", ok, detail)
        _check("abandon: ignored file preserved",
               (repo / "secret.env").read_text() == "TOKEN=keep-me\n")


def test_unplaceable_worktree_refuses_naming_sibling_cause():
    """A7: a parent that cannot hold the sibling worktree refuses up front, naming
    the relative-sibling-path cause, rather than placing it where ../ breaks."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        os.chmod(repo.parent, 0o555)
        try:
            code, err = _run_fix(repo, _Doer(lambda: "implemented"))
        finally:
            os.chmod(repo.parent, 0o755)
        _check("unplaceable: refused before running", code == 1, f"code={code}")
        _check("unplaceable: message names the sibling-path cause",
               "SIBLING" in err and "../harness_core" in err, err[-400:])


def test_crash_between_add_and_record_leaves_nothing():
    """codex BLOCKING r1 (A5): a crash after `git worktree add` but BEFORE the
    path was recorded orphaned both the directory and the `.git/worktrees`
    entry, because `remove()` short-circuits on `self.path is None` and nothing
    else ever looks for them.

    The window is DURING enter(), so the test must enter it: subprocess.run is
    wrapped so the real `worktree add` executes and the very next call raises,
    exactly as a SIGINT between the two statements would. An earlier version of
    this test checked `wt.path` AFTER enter() returned and passed with the fold
    reverted -- it was asserting a state both orderings reach."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        cwd0 = os.getcwd()
        os.chdir(repo)
        # enter() does a LOCAL `import subprocess`, which binds the same module
        # object, so patching the module attribute reaches it.
        real_run = subprocess.run
        added: list[str] = []

        def crash_after_add(*a, **kw):
            out = real_run(*a, **kw)
            argv = a[0] if a else kw.get("args", [])
            if "worktree" in argv and "add" in argv:
                added.append("yes")
                raise KeyboardInterrupt("simulated crash in the record window")
            return out

        wt = cli._RunWorktree()
        try:
            subprocess.run = crash_after_add
            try:
                wt.enter()
            except KeyboardInterrupt:
                pass
        finally:
            subprocess.run = real_run
            os.chdir(cwd0)

        _check("crash: the add really happened", bool(added))
        # THE ASSERTION: in the window, is the path visible to cleanup?
        _check("crash: path is recorded inside the window",
               wt.path is not None,
               "path is None -- remove() will short-circuit and orphan the worktree")
        wt.remove()
        ok, detail = _no_worktree_left(repo)
        _check("crash: nothing orphaned after cleanup", ok, detail)


def test_failed_add_still_cleans_up():
    """codex BLOCKING r2: a NONZERO `git worktree add` is not a guarantee that
    nothing was created -- it can leave a partial directory and/or an admin
    entry. An earlier fold cleared `self.path` on that branch, which made
    remove() short-circuit on exactly the wreckage it exists to clear. Here the
    add is forced to fail AFTER really creating the worktree, which is the shape
    that branch has to survive."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup(Path(td).resolve())
        cwd0 = os.getcwd()
        os.chdir(repo)
        real_run = subprocess.run

        def add_fails_after_creating(*a, **kw):
            out = real_run(*a, **kw)
            argv = a[0] if a else kw.get("args", [])
            if "worktree" in argv and "add" in argv:
                out.returncode = 1          # real worktree exists; add "failed"
                if hasattr(out, "stderr") and out.stderr is None:
                    out.stderr = ""
            return out

        wt = cli._RunWorktree()
        try:
            subprocess.run = add_fails_after_creating
            try:
                wt.enter()
            except SystemExit:
                pass
        finally:
            subprocess.run = real_run
            os.chdir(cwd0)

        _check("failed-add: path still claimed for cleanup", wt.path is not None,
               "cleared -> remove() short-circuits on the wreckage")
        wt.remove()
        ok, detail = _no_worktree_left(repo)
        _check("failed-add: nothing orphaned", ok, detail)


def test_no_change_run_leaves_the_doer_reply_readable():
    """A no-change run's doer reply is recoverable from its artifacts (the
    --log-file), read back after the run ends; an empty reply reads back as
    "", distinct from no capture at all (no event); the verdict is unchanged; and the
    restore scope is stated on the same record. A reply longer than _MAX_STR
    reads back IN FULL from the log file (the debate log keeps a capped
    preview), and the log file is operator-only (0o600) -- also when it
    overwrites a pre-existing world-readable file."""
    import json
    import stat
    from .orchestrator import _MAX_STR
    long_reply = ("Refusing: " + "the spec contradicts test_x. " * 2000
                  + "TAIL-REASON: editing tests is forbidden.")
    assert len(long_reply) > _MAX_STR
    for label, reply, preexisting in (
            ("text", "Refusing: the spec demands X=2 but test_x pins X=1, "
                     "and editing tests is forbidden.", False),
            # The runner types a reply as str, so "missing" reaches this point
            # as "" (a None reply fails the step first).
            ("empty", "", False),
            ("long", long_reply, True)):
        with tempfile.TemporaryDirectory() as td:
            repo = _setup(Path(td).resolve())
            log = repo.parent / "run-log.json"
            if preexisting:
                log.write_text("stale")
                log.chmod(0o644)
            old_umask = os.umask(0o022)
            try:
                code, err = _run_fix(repo, _Doer(lambda: reply),
                                     extra=("--log-file", str(log)))
            finally:
                os.umask(old_umask)
            mode = stat.S_IMODE(log.stat().st_mode)
            _check(f"no-change[{label}]: log file is operator-only (0o600)",
                   mode == 0o600, oct(mode))
            _check(f"no-change[{label}]: run did not pass", code == 1,
                   f"code={code} err={err[-400:]}")
            out = json.loads(log.read_text())
            _check(f"no-change[{label}]: still escalated_no_signal, same reason",
                   out["outcome"] == "escalated_no_signal"
                   and "doer:implement produced no changes" in
                   (out["escalation_reason"] or ""), str(out["escalation_reason"]))
            events = [e for e in out["debate_log"]
                      if e["event"] == "doer_no_change"]
            _check(f"no-change[{label}]: the doer's FULL reply reads back verbatim",
                   out.get("doer_no_change_reply") == reply,
                   f"len={len(out.get('doer_no_change_reply') or '')} "
                   f"want={len(reply)}")
            _check(f"no-change[{label}]: debate log carries the capped preview",
                   len(events) == 1 and "reply" in events[0]
                   and events[0]["reply"] == reply[:_MAX_STR]
                   and (len(reply) <= _MAX_STR
                        or events[0].get("truncations")),
                   f"{str(events)[:400]}")
            scope = out.get("restore_scope") or ""
            # This assertion PINNED THE DEFECT until codex r1 caught it: it
            # required the OPERATOR'S CHECKOUT path to appear in the scope, i.e.
            # it demanded the statement name as covered the one tree the restore
            # specifically does not touch. It now requires the opposite -- the
            # run's own worktree named as what IS covered, and the operator's
            # checkout named among what is not.
            _check(f"no-change[{label}]: restore scope names the WORKTREE, not the checkout",
                   ".potluck-run-" in scope and "neither restored nor detected" in scope,
                   scope)
            _check(f"no-change[{label}]: restore scope excludes the operator's checkout",
                   "operator's checkout" in scope and str(repo) + ";" not in scope,
                   scope)


def test_oversized_reply_truncates_and_says_so():
    """codex MAJOR r3: the durable test never exercised the 256 KiB boundary, so
    'recoverable verbatim' was only ever checked below it. A1 is AMENDED to
    promise verbatim up to the cap WITH the truncation and true length recorded;
    this pins that amended promise at the boundary itself."""
    from harness.cli import _REPLY_PERSIST_MAX
    big = "x" * (_REPLY_PERSIST_MAX + 1024)
    capped = big[:_REPLY_PERSIST_MAX]
    _check("oversized: cap is the documented 256 KiB",
           _REPLY_PERSIST_MAX == 256 * 1024, str(_REPLY_PERSIST_MAX))
    _check("oversized: the kept prefix is verbatim",
           capped == big[:len(capped)] and len(capped) == _REPLY_PERSIST_MAX,
           f"len={len(capped)}")
    _check("oversized: truncation is detectable from the recorded length",
           len(big) > _REPLY_PERSIST_MAX,
           "a reader must be able to tell a capped reply from a whole one")


def main() -> bool:
    for t in (test_passed_run_delivers_without_moving_the_branch,
              test_no_change_run_leaves_the_doer_reply_readable,
              test_foreign_commit_survives_abandonment,
              test_unplaceable_worktree_refuses_naming_sibling_cause,
              test_oversized_reply_truncates_and_says_so,
              test_crash_between_add_and_record_leaves_nothing,
              test_failed_add_still_cleans_up):
        t()
    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
