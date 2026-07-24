"""Resumable-run checkpoint engine (hardening Step-0 §2, potluck side).

`potluck fix` mutates the git tree, so a resumable checkpoint must reconstruct
the EXACT completed-round tree state, losslessly and verifiably (Ruling A: full
lossless tree-resume, no downgrade to history retention). This module owns:

  * the lossless git-tree SNAPSHOT/RESTORE recipe (§2.3), content-addressed via
    git OIDs -- both the working tree AND the real index -- with a submodule
    refusal and an index-canonicality guard that fail CLOSED;
  * atomic checkpoint file IO (LAST + temp-rename, §2.1) so a crash never leaves
    a half-written checkpoint;
  * the fail-closed identity recompute (§2.1): resume refuses on ANY divergence.

Design note on the subprocess layer. These are LOCAL, DETERMINISTIC git plumbing
calls that run at checkpoint/resume BOUNDARIES -- never inside a bounded model
call -- so they are deliberately synchronous `subprocess.run` and unbounded, the
same class as `real_run_gate`/`real_get_diff`'s local git tooling (orchestrator
§ "local deterministic git tooling"). Keeping them out of the async StepRunner
path is also what makes this engine unit-testable against a real temp git repo
with no event loop.

The typed shapes (ExecCursor, TreeSnapshot, CheckpointIdentity, RunCheckpoint,
Phase, CHECKPOINT_SCHEMA_VERSION) live in harness/schemas.py.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .schemas import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointIdentity,
    RunCheckpoint,
    TreeSnapshot,
)

# The declared-gap message (obs-017) when a target working tree carries a
# submodule or nested repo -- not resumable in v1, refused at checkpoint so a
# gitlink's dirty/untracked contents are never silently dropped (a false
# losslessness claim). Blast radius: only such repos, which the phase-1 mocks
# are not.
_SUBMODULE_GAP = (
    "submodule/nested-repo working trees are not resumable in v1; "
    "blast radius = only such repos, which the phase-1 mocks are not"
)

_CHECKPOINT_FILENAME = "checkpoint.json"


class CheckpointError(RuntimeError):
    """A checkpoint or restore refused (fail-closed). Never resume onto a
    divergent tree/index, a non-canonical index, a submodule, an unknown schema,
    or a mismatched identity: raise instead of proceeding."""


# --- git plumbing ------------------------------------------------------------

def _git(args: list[str], cwd: Path, *, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run one git plumbing command in `cwd`. Bytes in/out (paths may hold
    non-UTF-8). Raises CheckpointError on a non-zero exit when `check` (the
    stderr is surfaced, trimmed) -- a git failure mid-snapshot/restore must fail
    closed, never be inferred from empty stdout."""
    full_env = None
    if env is not None:
        full_env = {**os.environ, **env}
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise CheckpointError(
            f"git {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[:500]}"
        )
    return proc


def _fresh_index_path(tmpdir: str) -> str:
    """A freshly-created, NONEXISTENT temp path for GIT_INDEX_FILE (mktemp -u
    style). A bare zero-byte `mktemp` FILE is NOT a valid index (round-3 MINOR);
    the caller initializes it with `git read-tree --empty` first."""
    return os.path.join(tmpdir, "index")


# --- refusals: submodules and index canonicality -----------------------------

def _refuse_submodule_or_nested(repo: Path) -> None:
    """Refuse (fail-closed) if the working tree contains a submodule or a nested
    `.git` below the root (§2.3). A gitlink records only a commit id, not the
    submodule's dirty/untracked contents, so snapshotting it would be a false
    losslessness claim."""
    status = _git(["submodule", "status"], repo, check=False)
    if status.returncode == 0 and status.stdout.strip():
        raise CheckpointError(_SUBMODULE_GAP)
    # A `.git` dir/file BELOW the root (a nested repo/submodule work tree). The
    # root's own `.git` is expected and excluded.
    root_git = (repo / ".git").resolve()
    for dotgit in repo.rglob(".git"):
        if dotgit.resolve() == root_git:
            continue
        raise CheckpointError(_SUBMODULE_GAP)


def _refuse_noncanonical_index(repo: Path) -> None:
    """Refuse unless the REAL index is canonical stage-0 with none of the
    index-only flags `write-tree`+`read-tree` cannot round-trip (§2.3, round-5
    MAJOR): intent-to-add, skip-worktree, assume-unchanged, or unmerged (stage!=0).

    A checkpoint carrying any of them would restore a subtly different
    status/diff. Detected via `git ls-files -v` (a non-`H` tag => a
    skip-worktree/assume-unchanged/etc. flag), `git ls-files -u` (non-empty =>
    unmerged), and an intent-to-add probe. Blast radius (obs-017): potluck's fix
    loop only ever plain-`git add`s a mock repo, so the guard never fires in
    practice; it is a fail-closed catch for an unexpected index, not a live
    limitation."""
    unmerged = _git(["ls-files", "-u"], repo)
    if unmerged.stdout.strip():
        raise CheckpointError(
            "checkpoint refused: the real index has unmerged (stage!=0) entries, "
            "which write-tree/read-tree cannot round-trip"
        )
    # `ls-files -v`: each line is `<tag> <path>`. A canonical, up-to-date
    # cached entry is tagged `H`. Lowercase tags (h/s/m/r/c/k) and other
    # letters mark skip-worktree / assume-unchanged / and similar index-only
    # state. Anything but `H` refuses.
    verbose = _git(["ls-files", "-v"], repo)
    for raw in verbose.stdout.split(b"\n"):
        if not raw:
            continue
        tag = raw[:1]
        if tag != b"H":
            raise CheckpointError(
                "checkpoint refused: the real index carries a non-canonical "
                f"entry (ls-files tag {tag.decode(errors='replace')!r}); "
                "skip-worktree/assume-unchanged/etc. cannot be round-tripped"
            )
    # Intent-to-add probe: a `git add -N` entry records the path in the index
    # with a zero object + zero index-mode but a live working-tree mode, so
    # `git status --porcelain=v2` emits it as a `.A` changed entry (index
    # unmodified from HEAD, worktree added) -- distinct from a real staged add
    # (`A.`). `write-tree` projects it as the empty blob, so read-tree cannot
    # reproduce the intent-to-add flag; refuse. (`ls-files -v` tags an ITA entry
    # `H`, so it must be caught here, not by the tag scan above.)
    status = _git(["status", "--porcelain=v2"], repo)
    for raw in status.stdout.split(b"\n"):
        if not raw.startswith((b"1 ", b"2 ")):
            continue
        xy = raw.split(b" ", 2)[1]
        if xy == b".A":
            raise CheckpointError(
                "checkpoint refused: the real index has an intent-to-add "
                "(git add -N) entry, which write-tree/read-tree cannot round-trip"
            )


# --- snapshot ----------------------------------------------------------------

def snapshot_tree(repo: Path) -> TreeSnapshot:
    """Capture a lossless, content-addressed snapshot of the working tree AND the
    real index at a resumable checkpoint (§2.3). Fails closed on a submodule /
    nested repo (before anything else) and on a non-canonical index.

    `work_tree_oid` covers tracked + untracked + ignored blobs, exec/symlink
    modes, and binary bytes (`add -A --force` defeats `.gitignore`), captured
    through a FRESH temp index so the real index is untouched. `index_tree_oid`
    is `write-tree` on the REAL index (whatever the loop had staged)."""
    repo = Path(repo)
    _refuse_submodule_or_nested(repo)
    _refuse_noncanonical_index(repo)

    base = _git(["rev-parse", "HEAD"], repo).stdout.decode().strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        # Initialize the temp index (NOT a bare empty file, round-3 MINOR), then
        # stage the ENTIRE working tree into it and write its tree.
        _git(["read-tree", "--empty"], repo, env=wenv)
        _git(["add", "-A", "--force"], repo, env=wenv)
        work_tree_oid = _git(["write-tree"], repo, env=wenv).stdout.decode().strip()

    # The REAL index's tree projection (the checkpoint-time staged state).
    index_tree_oid = _git(["write-tree"], repo).stdout.decode().strip()

    digest = _tree_digest(repo, work_tree_oid)
    return TreeSnapshot(
        repo_base_commit=base,
        work_tree_oid=work_tree_oid,
        index_tree_oid=index_tree_oid,
        tree_snapshot_digest=digest,
    )


def _tree_digest(repo: Path, tree_oid: str) -> str:
    """A canonical digest over `git ls-tree -r <oid>` (§2.3). Content-addressed
    already, but the digest gives a single stable fingerprint the identity binds
    and restore re-verifies."""
    listing = _git(["ls-tree", "-r", "-z", tree_oid], repo).stdout
    return hashlib.sha256(listing).hexdigest()


# --- restore -----------------------------------------------------------------

def restore_tree(repo: Path, tree: TreeSnapshot) -> None:
    """Reestablish BOTH the working tree AND the real index to the exact
    checkpoint-time state (§2.3), deletion-correct and free of directory/file
    conflicts. Refuses (never resumes onto a divergent tree/index) on: a dirty
    tree, a base mismatch, a submodule, an incomplete capture, or either
    restore-digest mismatch.

    The order matters and each step is load-bearing:
      1. require a clean tree AT repo_base_commit, re-refuse submodules, hard-reset;
      2. empty the working tree WITHOUT touching the real index (D/F fix);
      3. materialize work_tree_oid into the empty tree via a fresh temp index;
      4. reestablish the REAL index to index_tree_oid;
      5. verify BOTH (content AND index-semantics), refuse on either mismatch.
    """
    repo = Path(repo)

    # (1) Precondition: a clean tree at exactly the base commit. Nothing
    # uncommitted is lost because we require it clean first.
    _refuse_submodule_or_nested(repo)
    head = _git(["rev-parse", "HEAD"], repo).stdout.decode().strip()
    if head != tree.repo_base_commit:
        raise CheckpointError(
            f"restore refused: HEAD ({head[:12]}) is not the checkpoint base "
            f"commit ({tree.repo_base_commit[:12]})"
        )
    status = _git(["status", "--porcelain"], repo)
    if status.stdout.strip():
        raise CheckpointError(
            "restore refused: the working tree is not clean at the base commit "
            "(refusing to overwrite uncommitted work)"
        )
    _git(["reset", "--hard", "--quiet", tree.repo_base_commit], repo)

    # (2) Empty the working tree WITHOUT touching the real index. `ls-files -z`
    # + a shell `xargs -0 rm -f --` (the option terminator, round-5 MINOR, so a
    # leading-`-` pathname can't be read as a flag), then `clean -fdx` for
    # untracked/ignored. The real index stays at base; no base path of one shape
    # can obstruct a snapshot entry of another (the D/F fix).
    _empty_worktree_keep_index(repo)

    # (3) Materialize the working-tree snapshot into the now-empty tree via a
    # fresh temp index. Working tree == snapshot; the REAL index is untouched.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        _git(["read-tree", "--empty"], repo, env=wenv)
        _git(["read-tree", tree.work_tree_oid], repo, env=wenv)
        _git(["checkout-index", "-a", "-f"], repo, env=wenv)

    # (4) Reestablish the REAL index to the checkpoint-time staged state.
    _git(["read-tree", tree.index_tree_oid], repo)

    # (5) Verify BOTH -- content (working tree) AND index-semantics (real index).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        _git(["read-tree", "--empty"], repo, env=wenv)
        _git(["add", "-A", "--force"], repo, env=wenv)
        got_work = _git(["write-tree"], repo, env=wenv).stdout.decode().strip()
    if got_work != tree.work_tree_oid:
        raise CheckpointError(
            "restore refused: recomputed working-tree oid "
            f"({got_work[:12]}) != snapshot ({tree.work_tree_oid[:12]})"
        )
    got_digest = _tree_digest(repo, got_work)
    if got_digest != tree.tree_snapshot_digest:
        raise CheckpointError(
            "restore refused: recomputed tree digest does not match the snapshot"
        )
    got_index = _git(["write-tree"], repo).stdout.decode().strip()
    if got_index != tree.index_tree_oid:
        raise CheckpointError(
            "restore refused: recomputed real-index oid "
            f"({got_index[:12]}) != snapshot ({tree.index_tree_oid[:12]})"
        )


def _empty_worktree_keep_index(repo: Path) -> None:
    """Delete every tracked file from disk (leaving the real index at base) then
    remove untracked/ignored, so the tree is empty for the D/F-safe materialize
    (§2.3 step 2). `xargs -0 rm -f --` carries the option terminator so a
    pathname beginning with `-` is never read as a flag."""
    ls = _git(["ls-files", "-z"], repo)
    if ls.stdout.strip(b"\0"):
        # Pipe NUL-delimited paths into `xargs -0 rm -f --`. The `--` is the
        # load-bearing option terminator (round-5 MINOR). Run via the shell so
        # the pipe + xargs compose exactly as the ratified recipe specifies.
        proc = subprocess.run(
            "git ls-files -z | xargs -0 rm -f --",
            cwd=str(repo), shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise CheckpointError(
                "restore refused: emptying the working tree failed: "
                f"{proc.stderr.decode(errors='replace')[:500]}"
            )
    # Remove untracked AND ignored (-x): the snapshot captured ignored files, so
    # restore must clear them and re-materialize the snapshot's version for an
    # EXACT tree. (This differs from the hygiene helper, which omits -x to
    # protect .venv/.env -- there the goal is a pristine base, here it is an
    # exact snapshot; blast radius is the mock repo under resume.)
    _git(["clean", "-fdx", "--quiet"], repo)


# --- identity ----------------------------------------------------------------

def config_digest(effective_config: dict) -> str:
    """A stable digest over the resolved timeouts + caps actually in force
    (§2.1). Resume refuses if this changed -- a config edit that would alter
    timeouts/caps must not silently apply to an in-flight run."""
    import json
    canonical = json.dumps(effective_config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def digest_text(text: str) -> str:
    """SHA-256 of a document/criteria string (input_document_digest,
    criteria_content_hash)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def verify_identity(committed: CheckpointIdentity,
                    recomputed: CheckpointIdentity) -> None:
    """Fail-closed identity check on resume (§2.1). Recompute each commitment
    from the run being resumed and REFUSE on ANY divergence -- a mutated
    criteria, a different input doc, an altered effective config, an unknown
    schema version, or (potluck) a base/tree mismatch. run_id/directory identity
    is NOT the binding (circular, round-1 MAJOR-2)."""
    if committed.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError(
            f"resume refused: checkpoint schema version {committed.schema_version} "
            f"!= supported {CHECKPOINT_SCHEMA_VERSION}"
        )
    fields = [
        ("criteria_content_hash", "the sealed criteria changed"),
        ("input_document_digest", "the input document changed"),
        ("effective_config_digest", "the effective config (timeouts/caps) changed"),
        ("run_manifest_id", "the run manifest id does not match"),
        ("repo_base_commit", "the repo base commit does not match"),
        ("tree_snapshot_digest", "the tree snapshot digest does not match"),
    ]
    for name, why in fields:
        if getattr(committed, name) != getattr(recomputed, name):
            raise CheckpointError(f"resume refused: {why}")


# --- checkpoint file IO ------------------------------------------------------

def write_checkpoint(run_dir: Path, ckpt: RunCheckpoint) -> Path:
    """Write the checkpoint LAST + temp-renamed (§2.1) so a crash never leaves a
    half-written checkpoint. `os.replace` is atomic within a directory."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / _CHECKPOINT_FILENAME
    tmp = run_dir / (_CHECKPOINT_FILENAME + ".tmp")
    tmp.write_text(ckpt.model_dump_json(indent=2))
    os.replace(tmp, dest)
    return dest


def load_checkpoint(run_dir: Path) -> RunCheckpoint:
    """Load and parse the checkpoint sidecar, refusing an unknown schema version
    (fail-closed, §2.1). Does NOT verify identity -- the caller recomputes the
    commitment set from the live run and calls verify_identity()."""
    path = Path(run_dir) / _CHECKPOINT_FILENAME
    if not path.exists():
        raise CheckpointError(f"no checkpoint to resume at {path}")
    ckpt = RunCheckpoint.model_validate_json(path.read_text())
    if ckpt.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError(
            f"resume refused: checkpoint schema version {ckpt.schema_version} "
            f"!= supported {CHECKPOINT_SCHEMA_VERSION}"
        )
    return ckpt
