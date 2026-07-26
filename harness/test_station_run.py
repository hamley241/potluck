"""Tests for the §4f station-run artifact — potluck's station-emitted provenance.

The load-bearing property is that `output_tree_digest` EQUALS what reviewgate will
later recompute over the committed tree: the `git add -A` (gitignore-respecting) tree.
These run real git in a temp repo (no mocks) — the seam a stub cannot reach.
Runnable: `python3 -m harness.test_station_run`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from harness.checkpoint import _git, _tree_digest, digest_text
from harness.station_run import (
    STATION_RUN_CANON,
    STATION_RUN_FILENAME,
    StationRunArtifact,
    build_station_run_artifact,
    gate_digest_of_bytes,
    load_station_run_artifact,
    write_station_run_artifact,
)

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _init_repo(root: Path) -> str:
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "verify.sh").write_text("#!/bin/sh\necho ok\n")
    (root / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          stdout=subprocess.PIPE).stdout.decode().strip()


def _manual_add_a_tree_digest(root: Path) -> str:
    """Independently: `git add -A` into a temp index + write-tree + the ls-tree digest —
    the exact tree a commit of the current working state would produce."""
    with tempfile.TemporaryDirectory() as d:
        idx = os.path.join(d, "index")
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        subprocess.run(["git", "read-tree", "--empty"], cwd=root, env=env, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
        oid = subprocess.run(["git", "write-tree"], cwd=root, env=env,
                             stdout=subprocess.PIPE).stdout.decode().strip()
    return _tree_digest(root, oid)


def main() -> bool:
    # --- schema ---
    art = StationRunArtifact(
        spec_digest="a", acceptance_digest="b", gate_digest="c",
        base_commit="d", terminal_state="passed", output_tree_digest="e")
    _check("schema_defaults", art.schema_version == 1 and art.canon_version == STATION_RUN_CANON)
    try:
        StationRunArtifact(spec_digest="a", acceptance_digest="b", gate_digest="c",
                           base_commit="d", terminal_state="passed",
                           output_tree_digest="e", bogus=1)
        _check("schema_extra_forbidden", False, "accepted extra field")
    except Exception:
        _check("schema_extra_forbidden", True)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "repo"
        root.mkdir()
        base = _init_repo(root)

        # A run leaves an OUTPUT: a new tracked-able file + an ignored file.
        (root / "feature.py").write_text("print('done')\n")
        (root / ".gitignore").write_text("secret.env\n")
        (root / "secret.env").write_text("IGNORED\n")  # must NOT affect the digest

        gate_dig = gate_digest_of_bytes((root / ".claude" / "verify.sh").read_bytes())
        art = build_station_run_artifact(
            root, "the spec", "the acceptance", gate_dig, base, "passed")

        # --- input digests recompute independently ---
        _check("spec_digest", art.spec_digest == digest_text("the spec"))
        _check("acceptance_digest", art.acceptance_digest == digest_text("the acceptance"))
        # gate digest is over the exact BYTES (not a lossy decode).
        _check("gate_digest",
               art.gate_digest == gate_digest_of_bytes(b"#!/bin/sh\necho ok\n"))
        _check("base_commit", art.base_commit == base)
        _check("terminal_state", art.terminal_state == "passed")

        # --- output_tree_digest == the add-A tree digest (what a commit produces) ---
        _check("output_tree_matches_add_A",
               art.output_tree_digest == _manual_add_a_tree_digest(root),
               f"{art.output_tree_digest} != manual")

        # --- ignored files are EXCLUDED: removing the ignored file leaves the digest
        #     unchanged (a commit would not have contained it) ---
        (root / "secret.env").unlink()
        art2 = build_station_run_artifact(
            root, "the spec", "the acceptance", gate_dig, base, "passed")
        _check("ignored_excluded",
               art2.output_tree_digest == art.output_tree_digest,
               "ignored file changed the output tree digest")

        # --- a real output change DOES change the digest ---
        (root / "feature.py").write_text("print('changed')\n")
        art3 = build_station_run_artifact(
            root, "the spec", "the acceptance", gate_dig, base, "passed")
        _check("output_change_shifts_digest",
               art3.output_tree_digest != art.output_tree_digest)

        # --- write + load round-trip ---
        out = Path(d) / "run"
        write_station_run_artifact(out, art)
        loaded = load_station_run_artifact(out)
        _check("roundtrip", loaded == art)
        _check("file_written", (out / "station-run.json").is_file())

        # --- version pin: a future v2/unknown artifact is REFUSED by the v1 reader
        #     (Literal pins, fail-closed) rather than mis-read as v1 ---
        import json as _json

        def _write_mutated(dirp: Path, **overrides):
            dirp.mkdir()
            obj = _json.loads(art.model_dump_json())
            obj.update(overrides)
            (dirp / STATION_RUN_FILENAME).write_text(_json.dumps(obj))

        _write_mutated(Path(d) / "v2", schema_version=2)
        try:
            load_station_run_artifact(Path(d) / "v2")
            _check("version_pin_refuses_v2", False, "v1 reader accepted schema_version 2")
        except Exception:
            _check("version_pin_refuses_v2", True)
        _write_mutated(Path(d) / "badcanon", canon_version="some-other-canon-v9")
        try:
            load_station_run_artifact(Path(d) / "badcanon")
            _check("version_pin_refuses_canon", False, "accepted unknown canon_version")
        except Exception:
            _check("version_pin_refuses_canon", True)

    ok_all = True
    for name, ok, detail in _results:
        ok_all = ok_all and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ' + detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
