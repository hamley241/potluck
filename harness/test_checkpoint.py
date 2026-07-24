"""Lossless git-tree checkpoint/resume engine (hardening Step-0 §2.3/§2.1).

Real git against throwaway temp repos (the engine is deliberately synchronous
local plumbing, so it needs no event loop). Covers the ratified tripwires:

  T3b -- restore is lossless + verified + DELETION-correct + D/F-safe: a snapshot
         over a tree with binary + ignored + deleted + mode-changed + symlink
         files AND a directory/file transition (base `a/x` -> file `a`, base file
         `b` -> `b/y`) restores to a tree whose recomputed write-tree == the
         committed work_tree_oid; the REAL index is reestablished + verified; a
         corrupted snapshot refuses; a submodule/nested `.git` REFUSES at
         checkpoint; a NON-canonical index (skip-worktree/intent-to-add) REFUSES;
         a tracked path beginning with `-` does not break the empty step.
  T3   -- resume is fail-closed on identity: a mutated criteria hash, changed
         input doc, altered effective config, unknown schema, or base/tree
         mismatch refuses.
  IO   -- checkpoint write is atomic (temp-rename) + round-trips; load refuses a
         missing file and an unknown schema version.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import checkpoint as ck
from harness.schemas import (
    CHECKPOINT_SCHEMA_VERSION, CheckpointIdentity, ExecCursor, Phase,
    RunCheckpoint, TreeSnapshot,
)


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode()[:400]}")
    return proc.stdout


def _init_repo(root: Path):
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "commit.gpgsign", "false"], root)


def _refuses(fn) -> bool:
    try:
        fn()
        return False
    except ck.CheckpointError:
        return True


def _build_base_and_midrun(root: Path):
    """Base commit, then mutate the working tree into a 'mid-run' state that
    exercises every T3b edge, and return the captured snapshot."""
    _init_repo(root)
    # --- base tree ---
    (root / "a").mkdir()
    (root / "a" / "x").write_text("ax\n")          # dir a with a file (D/F src)
    (root / "b").write_text("bfile\n")             # file b (D/F src -> becomes dir)
    (root / "del.txt").write_text("delete me\n")   # to be deleted mid-run
    (root / "mode.sh").write_text("#!/bin/sh\n")   # to be chmod +x mid-run
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "-dash.txt").write_text("leading dash\n")  # leading-`-` tracked path
    _git(["add", "-A", "--", "a", "b", "del.txt", "mode.sh", ".gitignore"], root)
    _git(["add", "--", "-dash.txt"], root)
    _git(["commit", "-qm", "base"], root)

    # --- mid-run mutations ---
    (root / "del.txt").unlink()                    # deleted
    os.chmod(root / "mode.sh", 0o755)              # mode change (100644 -> 100755)
    (root / "ignored.txt").write_text("ignored content\n")  # ignored (captured via --force)
    (root / "bin.dat").write_bytes(b"\x00\x01\x02NUL\x00binary\xff")  # binary untracked
    os.symlink("b", root / "link")                 # symlink (before b becomes a dir)
    # D/F transition: a/x (dir a) -> file a
    (root / "a" / "x").unlink()
    (root / "a").rmdir()
    (root / "a").write_text("now a file\n")
    # D/F transition: file b -> dir b with b/y
    (root / "b").unlink()
    (root / "b").mkdir()
    (root / "b" / "y").write_text("byy\n")
    # Stage a plain change so the REAL index is non-empty but canonical.
    (root / "mode.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(root / "mode.sh", 0o755)
    _git(["add", "--", "mode.sh"], root)

    return ck.snapshot_tree(root)


def main() -> bool:
    results = {}

    # ================= T3b: lossless snapshot + restore =================
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        snap = _build_base_and_midrun(root)
        results["snapshot_has_both_oids"] = bool(
            snap.work_tree_oid and snap.index_tree_oid
            and snap.work_tree_oid != snap.index_tree_oid)

        # Reset to a clean base, then restore the mid-run state.
        _git(["reset", "--hard", "-q", snap.repo_base_commit], root)
        _git(["clean", "-fdxq"], root)
        ck.restore_tree(root, snap)

        # Recomputed working-tree oid matches (the engine already verifies this;
        # re-assert independently here).
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "index")
            env = {**os.environ, "GIT_INDEX_FILE": idx}
            subprocess.run(["git", "read-tree", "--empty"], cwd=root, env=env)
            subprocess.run(["git", "add", "-A", "--force"], cwd=root, env=env)
            got = subprocess.run(["git", "write-tree"], cwd=root, env=env,
                                 stdout=subprocess.PIPE).stdout.decode().strip()
        results["restore_worktree_oid_matches"] = (got == snap.work_tree_oid)

        # Deletion-correct: del.txt is absent after restore.
        results["deleted_absent_after_restore"] = not (root / "del.txt").exists()
        # D/F transitions materialized: a is a file, b is a dir with b/y.
        results["df_a_is_file"] = (root / "a").is_file()
        results["df_b_is_dir"] = (root / "b").is_dir() and (root / "b" / "y").is_file()
        # Ignored + binary + symlink survived.
        results["ignored_restored"] = (root / "ignored.txt").read_text() == "ignored content\n"
        results["binary_restored"] = (root / "bin.dat").read_bytes()[:4] == b"\x00\x01\x02N"
        results["symlink_restored"] = (root / "link").is_symlink()
        # Mode change survived (mode.sh is executable).
        results["mode_restored"] = os.access(root / "mode.sh", os.X_OK)
        # Real index reestablished: its write-tree == index_tree_oid.
        real_idx = _git(["write-tree"], root).decode().strip()
        results["real_index_matches"] = (real_idx == snap.index_tree_oid)

    # ================= T3b: corrupted / tampered snapshot refuses =======
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        snap = _build_base_and_midrun(root)
        _git(["reset", "--hard", "-q", snap.repo_base_commit], root)
        _git(["clean", "-fdxq"], root)
        # A wrong tree_snapshot_digest (oids valid) -> step-5 digest mismatch.
        bad_digest = TreeSnapshot(**{**snap.model_dump(),
                                     "tree_snapshot_digest": "0" * 64})
        results["tampered_digest_refuses"] = _refuses(
            lambda: ck.restore_tree(root, bad_digest))

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        snap = _build_base_and_midrun(root)
        _git(["reset", "--hard", "-q", snap.repo_base_commit], root)
        _git(["clean", "-fdxq"], root)
        # A CORRUPT index_tree_oid (well-formed but nonexistent) -> step-4
        # `read-tree` fails, refusing rather than reestablishing a bad index. (A
        # merely *different valid* tree would round-trip cleanly and is a
        # different checkpoint, not corruption -- so the fail-closed guarantee is
        # about unreadable/partial captures, which this exercises.)
        bad_index = TreeSnapshot(**{**snap.model_dump(),
                                    "index_tree_oid": "1" * 40})
        results["tampered_index_oid_refuses"] = _refuses(
            lambda: ck.restore_tree(root, bad_index))

    # ================= T3b: restore refuses a dirty / wrong-base tree ====
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        snap = _build_base_and_midrun(root)
        _git(["reset", "--hard", "-q", snap.repo_base_commit], root)
        _git(["clean", "-fdxq"], root)
        (root / "dirty").write_text("uncommitted\n")  # dirty tree
        results["restore_refuses_dirty"] = _refuses(
            lambda: ck.restore_tree(root, snap))

    # ================= T3b: submodule / nested .git REFUSES at checkpoint =
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        _init_repo(root)
        (root / "f.txt").write_text("x\n")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "base"], root)
        nested = root / "vendor" / "lib"
        nested.mkdir(parents=True)
        _init_repo(nested)  # a nested .git below the root
        results["nested_git_refuses"] = _refuses(lambda: ck.snapshot_tree(root))

    # ================= T3b: non-canonical index REFUSES =================
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        _init_repo(root)
        (root / "f.txt").write_text("x\n")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "base"], root)
        _git(["update-index", "--skip-worktree", "f.txt"], root)
        results["skip_worktree_refuses"] = _refuses(lambda: ck.snapshot_tree(root))

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        _init_repo(root)
        (root / "f.txt").write_text("x\n")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "base"], root)
        (root / "new.txt").write_text("n\n")
        _git(["add", "-N", "new.txt"], root)  # intent-to-add
        results["intent_to_add_refuses"] = _refuses(lambda: ck.snapshot_tree(root))

    # ================= T3: identity fail-closed =========================
    ident = CheckpointIdentity(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        criteria_content_hash="crit", input_document_digest="doc",
        effective_config_digest="cfg", run_manifest_id="mid",
        repo_base_commit="base", tree_snapshot_digest="td")
    # Identical recompute passes.
    same = CheckpointIdentity(**ident.model_dump())
    ok = True
    try:
        ck.verify_identity(ident, same)
    except ck.CheckpointError:
        ok = False
    results["identity_match_ok"] = ok
    # Each divergence refuses.
    for fld, val in [("criteria_content_hash", "MUT"), ("input_document_digest", "MUT"),
                     ("effective_config_digest", "MUT"), ("run_manifest_id", "MUT"),
                     ("repo_base_commit", "MUT"), ("tree_snapshot_digest", "MUT")]:
        other = CheckpointIdentity(**{**ident.model_dump(), fld: val})
        results[f"identity_refuses_{fld}"] = _refuses(
            lambda o=other: ck.verify_identity(ident, o))
    # Unknown schema version refuses.
    bad_ver = CheckpointIdentity(**{**ident.model_dump(),
                                    "schema_version": CHECKPOINT_SCHEMA_VERSION + 99})
    results["identity_refuses_schema"] = _refuses(
        lambda: ck.verify_identity(bad_ver, bad_ver))

    # ================= IO: atomic write + load round-trip ===============
    with tempfile.TemporaryDirectory() as d:
        run_dir = Path(d) / "run"
        ckpt = RunCheckpoint(
            cursor=ExecCursor(phase=Phase.APPLY_FIXES, round_index=3,
                              failed_step="doer:apply_fixes", attempt=1),
            rounds_used=3, identity=ident,
            tree=TreeSnapshot(repo_base_commit="base", work_tree_oid="w",
                              index_tree_oid="i", tree_snapshot_digest="td"),
            state={"unresolved": [], "accepted": ["c1"]})
        ck.write_checkpoint(run_dir, ckpt)
        # No leftover temp file (atomic replace).
        results["io_no_temp_leftover"] = not (run_dir / "checkpoint.json.tmp").exists()
        loaded = ck.load_checkpoint(run_dir)
        results["io_roundtrip_cursor"] = (
            loaded.cursor.phase == Phase.APPLY_FIXES
            and loaded.cursor.round_index == 3
            and loaded.cursor.attempt == 1)
        results["io_roundtrip_state"] = (loaded.state == {"unresolved": [], "accepted": ["c1"]})
        # Missing checkpoint refuses.
        results["io_missing_refuses"] = _refuses(
            lambda: ck.load_checkpoint(Path(d) / "nope"))
        # Unknown on-disk schema version refuses.
        (run_dir / "checkpoint.json").write_text(
            ckpt.model_copy(update={"schema_version": CHECKPOINT_SCHEMA_VERSION + 5})
            .model_dump_json())
        results["io_bad_schema_refuses"] = _refuses(lambda: ck.load_checkpoint(run_dir))

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
