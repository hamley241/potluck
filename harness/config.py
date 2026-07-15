"""Harness configuration.

Every threshold is a config value, not a hardcoded constant. The first few real
runs tell you the right numbers far better than a guess -- so tune these in the
profile TOML, don't edit the orchestrator.

Loading order: built-in defaults <- profile TOML <- machine-local resolved TOML
<- environment overrides.

The machine-local resolved TOML (`.resolved.toml`, gitignored) holds the *model
backends for THIS machine* -- which CLI/path backs the reviewer and tiebreaker
roles. It is written by `potluck resolve` (see harness/resolve.py) and never
committed, exactly like credentials: model paths differ per machine, defaults
must stay portable.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Timeouts:
    # Per external-model call. Slow providers are common.
    model_call_seconds: int = 120
    # Per verification-gate run. Build/test can legitimately take a while;
    # tune per project -- this default is deliberately generous.
    gate_seconds: int = 300
    # Per debate round (a round may involve several calls).
    round_seconds: int = 400
    # Outer bound on a single feature across all rounds/retries.
    feature_seconds: int = 1800  # 30 min


@dataclass
class DebateConfig:
    # Hard ceiling on rounds. NOT a target -- early exit is expected.
    max_rounds: int = 2
    # Use the tiebreaker model on deadlocked blocking issues?
    use_tiebreaker: bool = True
    # 2-of-3 proceeds; a 3-way split escalates to human.
    # (When tiebreaker is off, ANY unresolved blocking issue escalates.)


@dataclass
class EscalationConfig:
    # A single timeout is retried once, transparently.
    retries_before_counting: int = 1
    # Escalate if this many timeouts accumulate within one feature...
    timeout_count_threshold: int = 3
    # ...OR if the SAME step times out this many times in a row.
    # (Consecutive same-step timeouts usually mean a broken environment,
    #  which is a different signal from scattered provider slowness.)
    consecutive_same_step_threshold: int = 2


@dataclass
class Backend:
    """One resolved model backend for a role.

    `cmd` is a CLI invocation template; the reply comes back on stdout. `fmt`
    selects how we extract the model's message from that stdout (Codex wraps it
    in JSONL; Claude and Kimi return plain text). `stdin` says how the prompt is
    delivered: True feeds it on stdin (Claude `-p`, Codex `exec`); False appends
    it as a final argv argument (Kimi `-p <prompt>`).
    """
    name: str = "claude"                                   # claude | codex | kimi
    cmd: list[str] = field(default_factory=lambda: ["claude", "-p"])
    fmt: str = "text"                                      # text | codex_jsonl
    stdin: bool = True


@dataclass
class Models:
    """Role -> backend mapping.

    The doer is Claude Code itself (this harness runs *around* it), so only the
    reviewer and tiebreaker are resolved here. The portable default is
    Claude-only -- three distinct Claude models across the three roles -- so a
    fresh clone runs with nothing installed but the `claude` CLI. `potluck
    resolve` overrides these (via .resolved.toml) with Codex/Kimi when present.
    """
    reviewer: Backend = field(
        default_factory=lambda: Backend("claude", ["claude", "-p", "--model", "sonnet"], "text")
    )
    tiebreaker: Backend = field(
        default_factory=lambda: Backend("claude", ["claude", "-p", "--model", "haiku"], "text")
    )


@dataclass
class HarnessConfig:
    profile: str = "personal"
    interactive: bool = True          # CI sets this False -> no human escalation target
    debate_enabled: bool = True       # CI disables -> deterministic gate only
    # Path-based routing: diffs touching these paths skip external review and
    # go human-only. Empty by default; flip on after checking with security.
    human_only_paths: list[str] = field(default_factory=list)

    timeouts: Timeouts = field(default_factory=Timeouts)
    debate: DebateConfig = field(default_factory=DebateConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    models: Models = field(default_factory=Models)

    @classmethod
    def load(cls, profile_path: Path | None = None,
             resolved_path: Path | None = None) -> "HarnessConfig":
        cfg = cls()
        if profile_path and profile_path.exists():
            with open(profile_path, "rb") as f:
                cfg._apply(tomllib.load(f))
        # Machine-local model resolution takes precedence over the profile.
        if resolved_path is None:
            resolved_path = Path(__file__).parent.parent / ".resolved.toml"
        if resolved_path and resolved_path.exists():
            with open(resolved_path, "rb") as f:
                cfg._apply(tomllib.load(f))
        cfg._apply_env()
        return cfg

    def _apply(self, data: dict) -> None:
        for k in ("profile", "interactive", "debate_enabled", "human_only_paths"):
            if k in data:
                setattr(self, k, data[k])
        for section, obj in (
            ("timeouts", self.timeouts),
            ("debate", self.debate),
            ("escalation", self.escalation),
        ):
            if section in data:
                for k, v in data[section].items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
        if "models" in data:
            self._apply_models(data["models"])

    def _apply_models(self, m: dict) -> None:
        for role in ("reviewer", "tiebreaker"):
            if role in m and isinstance(m[role], dict):
                b = m[role]
                setattr(self.models, role, Backend(
                    name=b.get("name", "claude"),
                    cmd=list(b.get("cmd", ["claude", "-p"])),
                    fmt=b.get("fmt", "text"),
                    stdin=b.get("stdin", True),
                ))

    def _apply_env(self) -> None:
        # A couple of high-value env overrides for CI.
        if os.environ.get("HARNESS_NONINTERACTIVE") == "1":
            self.interactive = False
        if os.environ.get("HARNESS_NO_DEBATE") == "1":
            self.debate_enabled = False

    def as_dict(self) -> dict:
        return asdict(self)
