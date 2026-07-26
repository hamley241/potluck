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
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

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


class StationRunArtifact(BaseModel):
    """A `potluck fix` run's canonical, station-emitted attestation (§4f). Every field
    is independently RECOMPUTABLE from the run's inputs + the repository, so a verifier
    trusts recomputation, never this record's say-so."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = STATION_RUN_SCHEMA_VERSION
    canon_version: str = STATION_RUN_CANON
    # Inputs (sha256 over the exact text / bytes the run consumed).
    spec_digest: str = Field(description="sha256 of the spec text")
    acceptance_digest: str = Field(description="sha256 of the acceptance text")
    gate_digest: str = Field(description="sha256 of the .claude/verify.sh gate")
    # Provenance.
    base_commit: str = Field(description="repo HEAD the run started from")
    terminal_state: str = Field(description="the FeatureResult outcome value")
    # The station OUTPUT — the `git add -A` (gitignore-respecting) tree the run left,
    # fingerprinted the way reviewgate will recompute reviewed_head^{tree}.
    output_tree_digest: str = Field(description="ls-tree digest of the output tree")


def _output_tree_digest(repo: Path) -> str:
    """The digest of the tree a `git add -A` + commit of the current working state
    would produce — ignored files EXCLUDED (no `--force`), so it equals what reviewgate
    recomputes over the committed tree. Staged into a FRESH temp index; the real index
    is untouched. Same `ls-tree -r` fingerprint the checkpoint engine uses."""
    repo = Path(repo)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_index = _fresh_index_path(tmpdir)
        wenv = {"GIT_INDEX_FILE": tmp_index}
        _git(["read-tree", "--empty"], repo, env=wenv)
        _git(["add", "-A"], repo, env=wenv)  # respects .gitignore (no --force)
        tree_oid = _git(["write-tree"], repo, env=wenv).stdout.decode().strip()
    return _tree_digest(repo, tree_oid)


def build_station_run_artifact(
    repo: Path, spec: str, acceptance: str, gate_path: Path,
    base_commit: str, terminal_state: str,
) -> StationRunArtifact:
    """Assemble the artifact from the run's inputs + the repository AFTER the run.
    `gate_path` is the `.claude/verify.sh` the run gated on; `base_commit` the HEAD it
    started from (captured before the run); `terminal_state` the FeatureResult outcome
    value."""
    gate_path = Path(gate_path)
    try:
        gate_bytes = gate_path.read_bytes()
    except OSError as e:
        raise CheckpointError(f"station-run: cannot read gate {gate_path}: {e}")
    return StationRunArtifact(
        spec_digest=digest_text(spec),
        acceptance_digest=digest_text(acceptance),
        gate_digest=digest_text(gate_bytes.decode("utf-8", errors="replace")),
        base_commit=base_commit,
        terminal_state=terminal_state,
        output_tree_digest=_output_tree_digest(repo),
    )


def write_station_run_artifact(out_dir: Path, artifact: StationRunArtifact) -> Path:
    """Write the artifact to `out_dir/station-run.json` (pretty, stable key order via
    the model). Returns the path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / STATION_RUN_FILENAME
    path.write_text(artifact.model_dump_json(indent=2) + "\n")
    return path


def load_station_run_artifact(out_dir: Path) -> StationRunArtifact:
    """Load + validate a persisted artifact, fail-closed on a missing/malformed file."""
    path = Path(out_dir) / STATION_RUN_FILENAME
    try:
        return StationRunArtifact.model_validate_json(path.read_text())
    except (OSError, ValueError) as e:
        raise CheckpointError(f"station-run: {path} unreadable/invalid: {e}")
