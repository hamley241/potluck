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

    `work_tree_oid` covers tracked + untracked-non-ignored blobs, exec/symlink
    modes, and binary bytes, captured through a FRESH temp index so the real
    index is untouched. `index_tree_oid` is `write-tree` on the REAL index
    (whatever the loop had staged).

    Ignored-file boundary (INTENTIONAL, matching `real_restore_tree`): ignored
    paths (`.venv`, `.env`, `__pycache__`, ...) are NOT captured. They are out of
    scope for the review diff and for hygiene, so they are out of scope for the
    snapshot too -- and restore_tree, which re-materialises only what is
    captured here, therefore never deletes or rewrites them."""
    repo = Path(repo)
    _refuse_submodule_or_nested(repo)
    _refuse_noncanonical_index(repo)

    base = _git(["rev-parse", "HEAD"], repo).stdout.decode().strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        work_tree_oid = _work_tree_oid(repo, tmp_index)

    # The REAL index's tree projection (the checkpoint-time staged state).
    index_tree_oid = _git(["write-tree"], repo).stdout.decode().strip()

    digest = _tree_digest(repo, work_tree_oid)
    return TreeSnapshot(
        repo_base_commit=base,
        work_tree_oid=work_tree_oid,
        index_tree_oid=index_tree_oid,
        tree_snapshot_digest=digest,
    )


def _work_tree_oid(repo: Path, tmp_index: str) -> str:
    """The tree OID of the working tree's tracked + untracked-non-ignored content,
    staged into the FRESH temp index at `tmp_index` (the real index is untouched).
    snapshot_tree and restore_tree's verification BOTH use this, so the restore
    is verified over exactly the scope the snapshot captured.

    The temp index is SEEDED FROM HEAD (`read-tree HEAD`, the station_run
    precedent), NOT empty: `.gitignore` affects UNTRACKED files only, so a file
    TRACKED yet matching `.gitignore` is retained, while an untracked ignored path
    is left out. An empty seed without `--force` would mis-drop the former."""
    wenv = {"GIT_INDEX_FILE": tmp_index}
    _git(["read-tree", "HEAD"], repo, env=wenv)
    _git(["add", "-A"], repo, env=wenv)
    return _git(["write-tree"], repo, env=wenv).stdout.decode().strip()


def _tree_digest(repo: Path, tree_oid: str) -> str:
    """A canonical digest over `git ls-tree -r <oid>` (§2.3). Content-addressed
    already, but the digest gives a single stable fingerprint the identity binds
    and restore re-verifies."""
    listing = _git(["ls-tree", "-r", "-z", tree_oid], repo).stdout
    return hashlib.sha256(listing).hexdigest()


def _snapshot_binding_digest(tree: TreeSnapshot) -> str:
    """A single fingerprint over the WHOLE TreeSnapshot -- base commit, working-tree
    oid, INDEX-tree oid, and the content digest -- so the identity binds every field
    restore will act on, not just the working-tree content (codex round-11 BLOCKING).

    tree_snapshot_digest alone covers only work_tree_oid, so binding it left
    index_tree_oid unbound: a tamperer could swap the real-index tree for another
    valid OID, pass identity verification, and make resume install a staged state
    different from the checkpointed run. Hashing all four fields closes that gap.
    NUL-delimited so no field boundary can be forged by concatenation."""
    payload = "\0".join([
        tree.repo_base_commit, tree.work_tree_oid,
        tree.index_tree_oid, tree.tree_snapshot_digest,
    ]).encode()
    return hashlib.sha256(payload).hexdigest()


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

    Ignored-file boundary (INTENTIONAL, the `real_restore_tree` ruling): restore
    PRESERVES ignored paths -- it never deletes or rewrites one. Step (2) uses
    `clean -fd`, never `-x`; step (3) uses `checkout-index` WITHOUT `-f`, which
    fails rather than remove an obstructing path; and an ignored path that would
    obstruct a snapshot entry is refused up front, before anything is touched.
    `-x` would delete `.venv`, `node_modules`, and `.env`.

    What this does and does not guarantee: the snapshot never captured ignored
    paths (see snapshot_tree), so restore is exact over the SAME scope it always
    claimed -- tracked + untracked-non-ignored content, plus the real index --
    and step (5) verifies exactly that scope, recomputed the same way it was
    captured. It guarantees NOTHING about ignored paths beyond leaving them
    untouched: whatever ignored files are present at resume (possibly differing
    from checkpoint time) stay byte-for-byte as they are.
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
    # Ignored paths are preserved, not deleted (see the docstring), so the old
    # "ignored files present" refusal is gone. What remains is one obstruction
    # case: an ignored path that sits where a snapshot entry must go (a file at
    # the entry's path or a parent of it, or a directory holding ignored files at
    # an entry's path). Refuse that HERE, before reset/empty touch anything, so
    # the refusal leaves the tree exactly as it was.
    _refuse_ignored_obstruction(repo, tree.work_tree_oid)
    _git(["reset", "--hard", "--quiet", tree.repo_base_commit], repo)

    # (2) Empty the working tree WITHOUT touching the real index. `ls-files -z`
    # + a shell `xargs -0 rm -f --` (the option terminator, round-5 MINOR, so a
    # leading-`-` pathname can't be read as a flag), after `clean -fd` for
    # untracked-non-ignored. The real index stays at base; no base path of one
    # shape can obstruct a snapshot entry of another (the D/F fix).
    _empty_worktree_keep_index(repo)

    # (3) Materialize the working-tree snapshot into the now-emptied tree via a
    # fresh temp index. Working tree == snapshot; the REAL index is untouched.
    # NO `-f`: with -f, checkout-index removes whatever obstructs an entry
    # (including a directory of ignored files); without it, it fails closed.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        _git(["read-tree", "--empty"], repo, env=wenv)
        _git(["read-tree", tree.work_tree_oid], repo, env=wenv)
        _git(["checkout-index", "-a"], repo, env=wenv)

    # (4) Reestablish the REAL index to the checkpoint-time staged state.
    _git(["read-tree", tree.index_tree_oid], repo)

    # (5) Verify BOTH -- content (working tree) AND index-semantics (real index).
    # The working tree is recomputed by the SAME function that captured it, so
    # the comparison covers exactly the snapshot's scope (ignored paths excluded
    # at both ends). HEAD is the base commit here (checked in step 1).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        got_work = _work_tree_oid(repo, tmp_index)
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


def _refuse_ignored_obstruction(repo: Path, work_tree_oid: str) -> None:
    """Refuse (before anything is touched) if a present ignored path collides
    with a snapshot entry: equal paths, an ignored path that is a parent of an
    entry, or an entry that is a parent of an ignored path. Materialising such an
    entry would have to remove the ignored path, which restore never does."""
    entries = set(_git(["ls-tree", "-r", "-z", "--name-only", work_tree_oid],
                       repo).stdout.split(b"\0")) - {b""}
    # Parent prefixes of every entry, so `a/b/c` collides with an ignored `a/b`.
    parents = {e[:i] for e in entries for i in range(len(e)) if e[i:i + 1] == b"/"}
    ignored = _git(["ls-files", "-z", "--others", "--ignored",
                    "--exclude-standard"], repo).stdout.split(b"\0")
    for path in ignored:
        if not path:
            continue
        hit = path in entries or path in parents or any(
            path[:i] in entries
            for i in range(len(path)) if path[i:i + 1] == b"/")
        if hit:
            raise CheckpointError(
                "restore refused: an ignored path obstructs a snapshot entry "
                f"({path.decode(errors='replace')!r}); restore never deletes "
                "ignored files -- move it aside explicitly to resume"
            )


def _empty_worktree_keep_index(repo: Path) -> None:
    """Remove untracked-non-ignored files, then delete every tracked file from
    disk (leaving the real index at base), so the tree is empty of everything the
    snapshot covers for the D/F-safe materialize (§2.3 step 2). Ignored paths
    are left in place. `xargs -0 rm -f --` carries the option terminator so a
    pathname beginning with `-` is never read as a flag."""
    # Remove untracked-non-ignored only. `-x` is deliberately NOT used, as in
    # `real_restore_tree`: it would delete `.venv`, `node_modules`, and `.env`.
    # The snapshot does not capture ignored paths, so leaving them costs the
    # restore no exactness over what it claims to restore. This runs FIRST, while
    # the tracked `.gitignore` files are still on disk: after the rm below they
    # are gone, and a `clean` then would see `.venv` as untracked and delete it.
    _git(["clean", "-fd", "--quiet"], repo)
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
        # The emptied parent dirs of tracked files stay (`rm` leaves them, and
        # `clean` ran before they were empty) and would obstruct a D/F
        # entry now that step (3) has no `-f`. Remove them deepest-first with
        # `rmdir`, which only ever removes an EMPTY directory: one still holding
        # ignored files is left alone.
        root = os.fsencode(str(repo))
        dirs = {p[:i] for p in ls.stdout.split(b"\0")
                for i in range(len(p)) if p[i:i + 1] == b"/"}
        for d in sorted(dirs, key=lambda p: p.count(b"/"), reverse=True):
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass


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
        ("gate_binding_digest", "the gate binding changed (gate downgrade/swap)"),
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


# --- run inputs (so resume can recompute the identity commitments) -----------

_INPUTS_FILENAME = "run_inputs.json"


def persist_run_inputs(run_dir: Path, spec: str, acceptance: str,
                       config_digest_value: str, repo_base_commit: str,
                       run_manifest_id: str) -> None:
    """Persist the run's identity inputs alongside the checkpoint so a `--resume`
    in a fresh process can RECOMPUTE the commitment set and fail closed on any
    divergence (§2.1). Written once at run start, atomically.

    NOTE (conductor §5): the gate binding is deliberately NOT stored here. run_inputs
    is a plain, editable sidecar; binding the gate via an editable field would let a
    resume downgrade the gate by editing this file (codex §5 r2 BLOCKING). The gate is
    instead bound into the tamper-evident CheckpointIdentity (`gate_binding_digest`)
    and resolved from a FILE FACT — the frozen copy's presence — never a mutable field."""
    import json
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "spec": spec, "acceptance": acceptance,
        "config_digest": config_digest_value,
        "repo_base_commit": repo_base_commit,
        "run_manifest_id": run_manifest_id,
    }
    tmp = run_dir / (_INPUTS_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, run_dir / _INPUTS_FILENAME)


def load_run_inputs(run_dir: Path) -> dict:
    import json
    path = Path(run_dir) / _INPUTS_FILENAME
    if not path.exists():
        raise CheckpointError(f"no run inputs to resume at {path}")
    return json.loads(path.read_text())


def compute_manifest_id(spec: str, acceptance: str, config_digest_value: str,
                        repo_base_commit: str) -> str:
    """A run's own manifest id, bound to the run's ACTUAL inputs (not the run_dir
    name -- that binding would be circular, round-1 MAJOR-2). Two runs over the
    same spec+acceptance+config+base share an id; any input change changes it."""
    joined = "\x00".join(
        [spec, acceptance, config_digest_value, repo_base_commit])
    return digest_text(joined)


# The gate binding for an IN-TREE run (conductor §5): a fixed, non-digest sentinel.
# The in-tree gate's bytes are already bound via tree_snapshot_digest, so the binding
# only needs to distinguish "in-tree mode" from any external gate's frozen digest —
# and a resume that lost/deleted the frozen copy recomputes THIS value, which cannot
# equal the external digest the checkpoint committed, so the downgrade is refused.
INTREE_GATE_BINDING = "in-tree"


def build_identity(spec: str, acceptance: str, config_digest_value: str,
                   repo_base_commit: str, tree_snapshot_digest: str,
                   run_manifest_id: str, gate_binding_digest: str) -> CheckpointIdentity:
    """The recomputed commitment set for a potluck run (§2.1). criteria = the
    acceptance criteria; input document = the spec; gate_binding_digest = the external
    gate's frozen-copy digest or INTREE_GATE_BINDING (conductor §5). Resume rebuilds
    this from the persisted inputs + the resolved gate and refuses on ANY field
    divergence (verify_identity)."""
    return CheckpointIdentity(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        criteria_content_hash=digest_text(acceptance),
        input_document_digest=digest_text(spec),
        effective_config_digest=config_digest_value,
        run_manifest_id=run_manifest_id,
        gate_binding_digest=gate_binding_digest,
        repo_base_commit=repo_base_commit,
        tree_snapshot_digest=tree_snapshot_digest,
    )


# --- checkpoint store (injectable: git-backed in prod, fake in tests) --------

class GitCheckpointStore:
    """Production checkpoint store: the real git-tree snapshot/restore engine
    (§2.3) plus atomic file IO in `run_dir`, over the target repo. Injected into
    the Orchestrator so the resume RE-ENTRY logic (cursor/state) is separable
    from -- and testable without -- the git tree mechanics (a FakeCheckpointStore
    drives the T2 re-entry tests; the tree engine is proven in test_checkpoint)."""

    def __init__(self, run_dir: Path, repo: Path | None = None):
        self.run_dir = Path(run_dir)
        self.repo = Path(repo) if repo is not None else Path.cwd()

    def snapshot(self) -> TreeSnapshot:
        return snapshot_tree(self.repo)

    def restore(self, tree: TreeSnapshot) -> None:
        restore_tree(self.repo, tree)

    def write(self, ckpt: RunCheckpoint) -> None:
        write_checkpoint(self.run_dir, ckpt)

    def load(self) -> RunCheckpoint:
        return load_checkpoint(self.run_dir)


class Checkpointer:
    """Drives checkpoint writes DURING a run and identity-verified restores on
    resume (§2). Bundles a store with the run-level identity inputs so the
    orchestrator can build a fail-closed identity per checkpoint from the current
    tree snapshot -- the orchestrator stays storage-agnostic (a FakeCheckpointStore
    with the same 4-method surface drives the T2 re-entry tests without git).

    `write` is called at the HEAD of each resumable call (§2.0: written before the
    call, so a timeout there resumes THAT call); it snapshots the tree, binds the
    identity, and writes atomically. `load_and_verify` recomputes the commitment
    set from the RESUMING process's inputs and refuses on any divergence (§2.1)."""

    def __init__(self, store, spec: str, acceptance: str,
                 config_digest_value: str, run_manifest_id: str,
                 gate_binding_digest: str = INTREE_GATE_BINDING):
        self.store = store
        self.gate_binding_digest = gate_binding_digest
        self.spec = spec
        self.acceptance = acceptance
        self.config_digest = config_digest_value
        self.manifest_id = run_manifest_id
        self._last: RunCheckpoint | None = None

    def write(self, cursor, rounds_used: int, state: dict,
              calls_dispatched: int = 0) -> None:
        tree = self.store.snapshot()
        identity = build_identity(
            self.spec, self.acceptance, self.config_digest,
            tree.repo_base_commit, _snapshot_binding_digest(tree), self.manifest_id,
            self.gate_binding_digest)
        self._last = RunCheckpoint(
            cursor=cursor, rounds_used=rounds_used, calls_dispatched=calls_dispatched,
            identity=identity, tree=tree, state=state)
        self.store.write(self._last)

    def bump_calls_dispatched(self, n: int) -> None:
        """Re-write the LAST checkpoint with an updated cumulative dispatch count
        (§1.3, codex round-3). Called on the timeout path once the dispatch has
        actually happened (the runner incremented), so a timed-out attempt is
        persisted -- without the head write having blindly counted a call that
        might never dispatch."""
        if self._last is not None and n != self._last.calls_dispatched:
            self._last = self._last.model_copy(update={"calls_dispatched": n})
            self.store.write(self._last)

    def load_and_verify(self) -> RunCheckpoint:
        """Load the checkpoint and REFUSE (fail-closed) unless the resuming run's
        recomputed commitments match what was checkpointed (§2.1).

        Division of labour (codex round-1 MINOR -- stated honestly rather than
        overclaimed): the RUN-LEVEL commitments -- criteria, input doc, effective
        config, manifest id -- are genuinely RECOMPUTED here from the resuming
        process's inputs, so a mutated spec/acceptance/config refuses. The
        potluck TREE commitments (repo_base_commit + tree_snapshot_digest) are not
        re-derived from the repo here (that would be comparing the checkpoint to a
        copy of itself); their VALUES are enforced downstream by restore_tree's own
        fail-closed checks -- HEAD must equal repo_base_commit, and the materialised
        tree's recomputed write-tree + digest must equal work_tree_oid + the
        snapshot digest (dual verify).

        codex round-10/11 (BLOCKING folds): but restore trusts ckpt.tree, while the
        identity carries its OWN copies of the tree commitments -- so the identity
        must be BOUND to the tree it will restore, or a tamperer could swap
        ckpt.tree for another internally-valid snapshot (leaving identity untouched)
        and slip it past verification. We recompute the identity from ckpt.TREE via
        _snapshot_binding_digest -- a fingerprint over the WHOLE snapshot (base,
        work-oid, index-oid, content-digest), not just the working-tree content --
        so verify_identity refuses unless every field restore will act on matches,
        INCLUDING index_tree_oid (round-11: binding the content digest alone left
        the real-index tree unbound). Tree-to-identity binding is checked HERE, its
        absolute content correctness at restore."""
        ckpt = self.store.load()
        recomputed = build_identity(
            self.spec, self.acceptance, self.config_digest,
            ckpt.tree.repo_base_commit, _snapshot_binding_digest(ckpt.tree),
            self.manifest_id, self.gate_binding_digest)
        verify_identity(ckpt.identity, recomputed)
        return ckpt

    def restore(self, tree) -> None:
        self.store.restore(tree)
