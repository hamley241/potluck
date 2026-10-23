"""export — a PASSED `potluck fix` round's portable result (ruling 001, part 1 of 3).

A run told to export (`--export-dir`) applies NOTHING to the operator's working tree.
On PASSED it writes, into a caller-supplied directory outside the target's git tree:

  * `cumulative.patch` — a self-contained `git diff --binary` from the ORIGINAL task
    base to the complete output tree (the same add-A boundary `deliver()` and
    `station_run._output_tree_digest` draw: untracked-non-ignored in, ignored out);
  * `station-run.json` — the run's v1 artifact, byte-for-byte (v1 is NOT changed;
    all transport metadata lives in the manifest below);
  * `spec.txt`, `acceptance.txt`, `gate.sh` — the EXACT inputs whose sha256 are the
    artifact's spec/acceptance/gate digests;
  * `export-manifest.json` — versioned, naming task, run and round, repository identity,
    the original base commit, the imported-result digest (the manifest digest of the
    result an importing run started from, else an explicit `null`), the cumulative
    patch digest, the output tree
    OID and digest, the station artifact digest, and every payload's digest under a
    manifest-relative name.

The manifest is published LAST (atomically), so a directory with payloads but no
manifest is an incomplete export and `load_export` refuses it. A result is addressed
by its MANIFEST DIGEST and carries bytes — never a bare tree OID, which names a loose
object left unreachable once the run's worktree is removed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .station_run import STATION_RUN_FILENAME

EXPORT_CANON: str = "potluck-export-v1"
EXPORT_SCHEMA_VERSION: int = 1
MANIFEST_FILENAME: str = "export-manifest.json"
PATCH_FILENAME: str = "cumulative.patch"
SPEC_FILENAME: str = "spec.txt"
ACCEPTANCE_FILENAME: str = "acceptance.txt"
GATE_FILENAME: str = "gate.sh"
REQUIRED_PAYLOADS: tuple[str, ...] = (PATCH_FILENAME, STATION_RUN_FILENAME, SPEC_FILENAME,
                                      ACCEPTANCE_FILENAME, GATE_FILENAME)


class ExportError(Exception):
    """An export that cannot be written, or an export directory that is not acceptable."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a sibling temp file, following NO symlink on
    either name.

    codex r1 MAJOR: `Path.write_bytes` FOLLOWS a symlink, so a pre-existing
    `<payload>.tmp` pointing into the operator's checkout would have been written
    THROUGH — truncating a tracked operator file from the one code path whose whole
    purpose is to touch nothing outside the export directory, and then reporting a
    successful export. Validating the export directory could never catch it: the
    directory is legitimately outside the tree; it is the LINK inside it that escapes.

    The temp name is unlinked first (`os.unlink` removes the LINK, never its target)
    and then created `O_CREAT|O_EXCL|O_NOFOLLOW` 0600, so a planted link cannot be
    written through and a stale `.tmp` from a crashed run does not wedge the export.
    The final `os.replace` swaps the NAME, so a symlink at `path` is replaced rather
    than followed."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ExportError(f"cannot clear the temporary export file {tmp}: {e}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def clear_export(out_dir: Path) -> None:
    """Remove a stale manifest BEFORE the run, so a reused directory never offers a
    previous round's export as this run's result (a non-PASSED run leaves none)."""
    try:
        (Path(out_dir) / MANIFEST_FILENAME).unlink()
    except FileNotFoundError:
        pass


def write_export(out_dir: Path, *, task_id: str, run_id: str, round_index: int,
                 repository: dict,
                 base_commit: str, output_tree_oid: str, output_tree_digest: str,
                 patch: bytes, station_run_bytes: bytes, spec: str, acceptance: str,
                 gate_bytes: bytes, imported_result_digest: str | None = None) -> str:
    """Write every payload, then publish the manifest LAST. Returns the manifest digest.
    Spec/acceptance are encoded exactly as `checkpoint.digest_text` hashes them, so each
    payload's digest is the station artifact's binding for it. `imported_result_digest`
    is the manifest digest of the result an importing run started from, else None."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        PATCH_FILENAME: patch,
        STATION_RUN_FILENAME: station_run_bytes,
        SPEC_FILENAME: spec.encode("utf-8", errors="replace"),
        ACCEPTANCE_FILENAME: acceptance.encode("utf-8", errors="replace"),
        GATE_FILENAME: gate_bytes,
    }
    for name, data in payloads.items():
        _write_atomic(out_dir / name, data)
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "canon_version": EXPORT_CANON,
        "task_id": task_id,
        "run_id": run_id,
        "round": round_index,
        "repository": repository,
        "base_commit": base_commit,
        # The previous round's MANIFEST digest, or an explicit null for a non-importing run.
        "imported_result_digest": imported_result_digest,
        "cumulative_patch_digest": _sha(patch),
        "output_tree_oid": output_tree_oid,
        "output_tree_digest": output_tree_digest,
        "station_artifact_digest": _sha(station_run_bytes),
        "payloads": {name: _sha(data) for name, data in payloads.items()},
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    _write_atomic(out_dir / MANIFEST_FILENAME, raw)
    return _sha(raw)


_HEX40 = "0123456789abcdef"


def _require(manifest: dict, key: str, kind, out_dir: Path):
    """One manifest field, type-checked. `bool` is rejected where an int is required:
    `True == 1` in Python, so `schema_version: true` would otherwise pass a `== 1`
    comparison (codex r1 MAJOR, reproduced with an in-memory fixture)."""
    if key not in manifest:
        raise ExportError(f"export at {out_dir}: manifest names no {key}")
    v = manifest[key]
    if isinstance(v, bool) and kind is not bool:
        raise ExportError(f"export at {out_dir}: manifest {key} is a boolean")
    if not isinstance(v, kind):
        raise ExportError(f"export at {out_dir}: manifest {key} is {type(v).__name__}, "
                          f"expected {kind.__name__}")
    if kind is str and not v:
        raise ExportError(f"export at {out_dir}: manifest {key} is empty")
    return v


def _require_oid(manifest: dict, key: str, out_dir: Path) -> str:
    v = _require(manifest, key, str, out_dir)
    if len(v) != 40 or any(c not in _HEX40 for c in v):
        raise ExportError(f"export at {out_dir}: manifest {key} is not a 40-char "
                          f"lowercase hex OID: {v!r}")
    return v


def load_export(out_dir: Path) -> tuple[dict, str]:
    """Accept an export directory, fail-closed. The manifest must be present, be a JSON
    OBJECT, be of a known version, carry every transport field with the right type, list
    ALL five payloads under manifest-relative names whose bytes match their digests, and
    AGREE with the station artifact it ships: the artifact is parsed, and its
    `base_commit`, `output_tree_digest` and the three input digests must equal the
    manifest's base/output and the payload digests of spec/acceptance/gate.

    codex r1 MAJOR: the first version checked the task, the versions, the payload hashes
    and two redundant digests, and accepted a manifest with NO run/round/repository/base
    /output identity whose five payloads were the literal bytes `not a station artifact`
    — so a later importer treating "accepted" as "verified provenance" would have had
    none. Tree RECONSTRUCTION stays part 2; cross-file AGREEMENT is checkable here and so
    it is checked here. Returns (manifest, manifest digest)."""
    out_dir = Path(out_dir)
    try:
        raw = (out_dir / MANIFEST_FILENAME).read_bytes()
    except FileNotFoundError:
        raise ExportError(
            f"export at {out_dir} is not acceptable: {MANIFEST_FILENAME} is missing "
            f"(the manifest is published last, so the export never completed)")
    try:
        manifest = json.loads(raw)
    except ValueError as e:
        raise ExportError(f"export at {out_dir}: {MANIFEST_FILENAME} is not valid JSON: {e}")
    if not isinstance(manifest, dict):
        raise ExportError(f"export at {out_dir}: {MANIFEST_FILENAME} is a "
                          f"{type(manifest).__name__}, not a JSON object")
    if (_require(manifest, "schema_version", int, out_dir) != EXPORT_SCHEMA_VERSION
            or _require(manifest, "canon_version", str, out_dir) != EXPORT_CANON):
        raise ExportError(
            f"export at {out_dir}: unknown manifest version "
            f"{manifest.get('schema_version')!r}/{manifest.get('canon_version')!r}")
    _require(manifest, "task_id", str, out_dir)
    _require(manifest, "run_id", str, out_dir)
    _require(manifest, "round", int, out_dir)
    _require(manifest, "repository", dict, out_dir)
    _require_oid(manifest, "base_commit", out_dir)
    _require_oid(manifest, "output_tree_oid", out_dir)
    _require(manifest, "output_tree_digest", str, out_dir)
    _require(manifest, "cumulative_patch_digest", str, out_dir)
    _require(manifest, "station_artifact_digest", str, out_dir)
    if "imported_result_digest" not in manifest:
        raise ExportError(f"export at {out_dir}: manifest names no "
                          f"imported_result_digest (an explicit absence is `null`, "
                          f"not a missing key)")
    imported = manifest["imported_result_digest"]
    if imported is not None and not (isinstance(imported, str) and imported):
        raise ExportError(f"export at {out_dir}: manifest imported_result_digest is "
                          f"neither null nor a non-empty string")
    payloads = _require(manifest, "payloads", dict, out_dir)
    for required in REQUIRED_PAYLOADS:
        if required not in payloads:
            raise ExportError(f"export at {out_dir}: manifest names no {required}")
    for name, want in payloads.items():
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ExportError(f"export at {out_dir}: payload name {name!r} is not "
                              f"manifest-relative")
        try:
            got = _sha((out_dir / name).read_bytes())
        except OSError as e:
            raise ExportError(f"export at {out_dir}: payload {name} is unreadable: {e}")
        if got != want:
            raise ExportError(
                f"export at {out_dir}: payload {name} does not match its manifest "
                f"digest (manifest {want}, bytes {got})")
    if (manifest["cumulative_patch_digest"] != payloads[PATCH_FILENAME]
            or manifest["station_artifact_digest"] != payloads[STATION_RUN_FILENAME]):
        raise ExportError(f"export at {out_dir}: manifest digests disagree with its "
                          f"payload table")
    # The station artifact is PARSED and must agree with the manifest and the inputs
    # shipped beside it. `digest_text` hashes utf-8-with-replace and the gate digest is
    # over exact bytes, which is how write_export encodes each payload — so the
    # artifact's own input digests must equal the payload digests, byte for byte.
    try:
        art = json.loads((out_dir / STATION_RUN_FILENAME).read_bytes())
    except ValueError as e:
        raise ExportError(f"export at {out_dir}: {STATION_RUN_FILENAME} is not valid "
                          f"JSON: {e}")
    if not isinstance(art, dict):
        raise ExportError(f"export at {out_dir}: {STATION_RUN_FILENAME} is a "
                          f"{type(art).__name__}, not a JSON object")
    for art_key, want, what in (
            ("base_commit", manifest["base_commit"], "the manifest's base_commit"),
            ("output_tree_digest", manifest["output_tree_digest"],
             "the manifest's output_tree_digest"),
            ("spec_digest", payloads[SPEC_FILENAME], f"the digest of {SPEC_FILENAME}"),
            ("acceptance_digest", payloads[ACCEPTANCE_FILENAME],
             f"the digest of {ACCEPTANCE_FILENAME}"),
            ("gate_digest", payloads[GATE_FILENAME], f"the digest of {GATE_FILENAME}")):
        got = art.get(art_key)
        if got != want:
            raise ExportError(
                f"export at {out_dir}: {STATION_RUN_FILENAME} {art_key} is {got!r}, "
                f"which does not match {what} ({want!r})")
    return manifest, _sha(raw)
