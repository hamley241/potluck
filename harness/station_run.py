"""station_run — potluck's station-emitted run artifact (conductor Step-0 §4f).

The producer half of the §4 station-provenance chain, built FIRST (decider ruling,
2026-07-25: producer-first, so the reviewgate verifier is later written against a REAL
artifact format, not a hypothesised one). A `potluck fix` run EMITS a canonical,
SCHEMA-VERSIONED record of what it did — the run's INPUTS (spec/acceptance/gate
digests), the BASE it started from, its TERMINAL state, and the OUTPUT tree it
produced — so a later landing can be bound to a real station execution rather than a
conductor-authored claim.

This slice only PRODUCES the artifact; nothing yet requires or verifies it. The
reviewgate `station_run` receipt field that recomputes `output_tree_digest` against
`reviewed_head^{tree}` at push time is a separate, opt-in slice (§4 verifier).

`output_tree_digest` — the load-bearing field — is computed so it EQUALS what reviewgate
will later recompute over the committed tree: the tree a `git add -A` (respecting
`.gitignore`, i.e. NOT `--force`) then commit would produce, fingerprinted by the same
`ls-tree -r` digest the checkpoint engine uses. Ignored files are excluded because a
git commit excludes them; the conductor commits the potluck output with `git add -A`,
so the committed tree matches this digest. Computed through a FRESH temp index, so the
run's real index is untouched.

RECOMPUTABLE vs ATTESTED (codex slice-2 BLOCKING). Four fields are independently
RECOMPUTABLE — the input digests (spec/acceptance/gate, vs the committed handoff
bundle), `base_commit` (git), and `output_tree_digest` (git) — and a verifier trusts
recomputation, never the record. `terminal_state`, by contrast, depends on the run's
DYNAMICS (reviewer verdicts, debate) and is NOT recoverable from inputs + tree; it is
the station's ATTESTED outcome. Trusting it is exactly the Step-0 §4(f) DECLARED
LIMITATION (threat-model B, decider-ruled 2026-07-25): under the ruled threat — the
automated doer SKIPPING the station — the station's own terminal report is sound; a
deliberate operator fabrication is out of the threat model. The recomputable fields
carry the provenance chain; `terminal_state` is attested run metadata.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .checkpoint import (
    CheckpointError,
    _fresh_index_path,
    _git,
    _tree_digest,
    digest_text,
)

STATION_RUN_CANON: str = "potluck-station-run-v1"
STATION_RUN_SCHEMA_VERSION: int = 1
STATION_RUN_FILENAME: str = "station-run.json"


def gate_digest_of_bytes(gate_bytes: bytes) -> str:
    """sha256 over the gate file's EXACT bytes (codex slice-2 MAJOR — never a lossy
    UTF-8 decode, which would collapse distinct byte files to one digest)."""
    return hashlib.sha256(gate_bytes).hexdigest()


class StationRunArtifact(BaseModel):
    """A `potluck fix` run's canonical, station-emitted attestation (§4f). The input
    digests + base_commit + output_tree_digest are independently RECOMPUTABLE (a
    verifier trusts recomputation); `terminal_state` is the station's ATTESTED outcome
    (the Step-0 §4(f) declared limitation), not a recomputed value.

    `schema_version`/`canon_version` are `Literal`-pinned (codex slice-2 MAJOR): a v1
    reader REFUSES a future v2 artifact rather than mis-reading it as v1 — born
    versioned, fail-closed on an unknown version."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = STATION_RUN_SCHEMA_VERSION
    canon_version: Literal["potluck-station-run-v1"] = STATION_RUN_CANON
    # RECOMPUTABLE — input digests (sha256; the gate over its exact bytes).
    spec_digest: str = Field(description="sha256 of the spec text")
    acceptance_digest: str = Field(description="sha256 of the acceptance text")
    gate_digest: str = Field(description="sha256 of the gate bytes, captured at run start")
    # RECOMPUTABLE — provenance from git.
    base_commit: str = Field(description="repo HEAD the run started from")
    output_tree_digest: str = Field(description="ls-tree digest of the add-A output tree")
    # ATTESTED (not recomputable) — the station's own terminal report.
    terminal_state: str = Field(description="station-attested FeatureResult outcome")


def _output_tree_digest(repo: Path) -> str:
    """The digest of the tree a `git add -A` + commit of the current working state
    would produce — so it equals what reviewgate recomputes over the committed tree.

    The temp index is SEEDED FROM HEAD (`read-tree HEAD`), NOT empty (codex slice-2
    BLOCKING): real `git add -A` runs against the HEAD-backed index, so a file that is
    TRACKED yet matches `.gitignore` is retained and updated (`.gitignore` only affects
    UNTRACKED files). An empty-index seed would mis-drop such a tracked-then-ignored
    file, diverging from `reviewed_head^{tree}`. Seeded from HEAD, `add -A` then models
    a real commit: tracked files updated (incl. tracked-ignored), untracked-non-ignored
    added, deletions applied. Staged into a FRESH temp index; the real index is
    untouched. Same `ls-tree -r` fingerprint the checkpoint engine uses."""
    repo = Path(repo)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        _git(["read-tree", "HEAD"], repo, env=wenv)  # HEAD-backed, like real add -A
        _git(["add", "-A"], repo, env=wenv)          # .gitignore affects untracked only
        tree_oid = _git(["write-tree"], repo, env=wenv).stdout.decode().strip()
    return _tree_digest(repo, tree_oid)


def build_station_run_artifact(
    repo: Path, spec: str, acceptance: str, gate_digest: str,
    base_commit: str, terminal_state: str,
) -> StationRunArtifact:
    """Assemble the artifact from the run's inputs + the repository AFTER the run.
    `gate_digest` MUST be captured at run START (`gate_digest_of_bytes` on the gate
    file's bytes before the doer runs — the doer can rewrite `.claude/verify.sh`, so a
    run-end read would attest the modified gate, codex slice-2 MAJOR); `base_commit` the
    HEAD it started from; `terminal_state` the station's attested FeatureResult
    outcome. Only `output_tree_digest` is computed here (over the post-run tree)."""
    return StationRunArtifact(
        spec_digest=digest_text(spec),
        acceptance_digest=digest_text(acceptance),
        gate_digest=gate_digest,
        base_commit=base_commit,
        terminal_state=terminal_state,
        output_tree_digest=_output_tree_digest(repo),
    )


def clear_station_run_artifact(out_dir: Path) -> None:
    """Remove any stale `station-run.json` BEFORE a run starts (codex slice-2 MAJOR):
    a reused run directory must never leave a prior run's artifact behind for a run
    whose own emission then fails — an old `passed` could be mistaken for the latest."""
    path = Path(out_dir) / STATION_RUN_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_station_run_artifact(out_dir: Path, artifact: StationRunArtifact) -> Path:
    """Write the artifact to `out_dir/station-run.json` ATOMICALLY (temp + os.replace),
    so a crash mid-write never leaves a truncated or stale-mixed artifact. Returns the
    path."""
    import os
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / STATION_RUN_FILENAME
    tmp = out_dir / (STATION_RUN_FILENAME + ".tmp")
    tmp.write_text(artifact.model_dump_json(indent=2) + "\n")
    os.replace(tmp, path)
    return path


def load_station_run_artifact(out_dir: Path) -> StationRunArtifact:
    """Load + validate a persisted artifact, fail-closed on a missing/malformed file."""
    path = Path(out_dir) / STATION_RUN_FILENAME
    try:
        return StationRunArtifact.model_validate_json(path.read_text())
    except (OSError, ValueError) as e:
        raise CheckpointError(f"station-run: {path} unreadable/invalid: {e}")
