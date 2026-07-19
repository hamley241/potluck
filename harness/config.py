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

EXTRACTED IN PART to harness_core 2026-07-18 (engine extraction, step 3).
config.py is a SPLIT, and a PERMANENT one — the two harnesses' `Timeouts`,
`DebateConfig`, `Models` and `HarnessConfig` genuinely differ (potluck has
`gate_seconds` and `DiffConfig` for a subprocess gate menu lacks; menu has
`run_seconds` and `exhaustion_threshold` for a tripwire potluck lacks), so
forcing one shape on both would put profile knowledge in the core, which HC4
forbids. See AMENDMENTS A-3.

What moved: `EscalationConfig`, `Backend`, and the layering MOVES —
`read_layers`, `overlay_keys`, `overlay_sections`, `overlay_backend`. What
stayed: the composed shapes above, the role names `_apply_models` iterates,
the env knobs `_apply_env` reads, `validate()`'s invariants, and the default
resolved-file location — every one of them potluck vocabulary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from harness_core.config import (  # noqa: F401  (EscalationConfig/Backend re-exported)
    Backend, EscalationConfig, overlay_backend, overlay_keys, overlay_sections,
    read_layers,
)


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


# `EscalationConfig` and `Backend` are imported from harness_core above.
# Both were byte-identical in the two harnesses, which is exactly the bar
# A-3 set for the core owning a piece. They stay importable from here so no
# call site had to change.


@dataclass
class Models:
    """Role -> backend mapping.

    The doer is Claude Code itself (this harness runs *around* it), so only the
    reviewer and tiebreaker are resolved here. The portable default is
    Claude-only -- three distinct Claude models across the three roles -- so a
    fresh clone runs with nothing installed but the `claude` CLI. `potluck
    resolve` overrides these (via .resolved.toml) with Codex/Kimi when present.
    """
    # `--tools ""` disables all tools so the Claude reviewer/tiebreaker are
    # read-only judgment calls, matching resolve.claude_backend(). This is the
    # floor used when no .resolved.toml exists, so the read-only guarantee must
    # hold here too.
    reviewer: Backend = field(
        default_factory=lambda: Backend(
            "claude", ["claude", "-p", "--tools", "", "--model", "sonnet"], "text")
    )
    tiebreaker: Backend = field(
        default_factory=lambda: Backend(
            "claude", ["claude", "-p", "--tools", "", "--model", "haiku"], "text")
    )


@dataclass
class DiffConfig:
    # Untracked files above this size are omitted from the review diff with
    # a `# skipped: <path> (size N bytes)` marker (so absence isn't silent).
    # 4 MB catches accidentally-checked-in node_modules blobs and generated
    # artifacts without truncating anything a reviewer would actually read.
    # A repo with large legitimate untracked source files can raise this.
    max_untracked_bytes: int = 4 * 1024 * 1024
    # Untracked files whose first 8 KB contains a NUL byte are treated as
    # binary and omitted (with a `# skipped: <path> (binary)` marker).
    # Reviewers can't act on binary blobs and diffing them wastes budget.
    binary_probe_bytes: int = 8 * 1024


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
    diff: DiffConfig = field(default_factory=DiffConfig)

    @classmethod
    def load(cls, profile_path: Path | None = None,
             resolved_path: Path | None = None) -> "HarnessConfig":
        cfg = cls()
        # Where potluck keeps its machine-local resolution is potluck's
        # business; the core is handed paths, never asked to find them.
        if resolved_path is None:
            from .paths import resolved_path as _default_resolved
            resolved_path = _default_resolved()
        # Lowest precedence first: machine-local model resolution takes
        # precedence over the profile. The ORDER of this list is the layering.
        for layer in read_layers([profile_path, resolved_path]):
            cfg._apply(layer)
        cfg._apply_env()
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail-fast invariants. Called at end of `load()` so a broken config
        REFUSES TO START rather than producing subtle runtime races. Two
        properties made unrepresentable here that were previously hopes:

        1. `feature_seconds >= max(step timeouts)`. The debate log guarantees
           "a finer-grained signal is never overwritten by a coarser one":
           step-level TimeoutEscalation names WHICH step hung; ABORTED_BUDGET
           only says "too long overall." That precedence only holds when
           the step timeout can actually fire before the outer wall-clock --
           otherwise `asyncio.wait_for(feature_seconds)` cancels the inner
           task before TimeoutEscalation raises, and the finer signal is lost.
           Enforcing the inequality at load time makes the guarantee real.

        2. Non-negative timeout values. A zero or negative step timeout is a
           config mistake, not a valid "no bound"; explicit rejection at
           load beats mysterious immediate-fire escalations later.
        """
        t = self.timeouts
        for name in ("model_call_seconds", "gate_seconds", "round_seconds",
                     "feature_seconds"):
            v = getattr(t, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"timeouts.{name} must be a non-negative int, got {v!r}"
                )
        step_bounds = (t.model_call_seconds, t.gate_seconds, t.round_seconds)
        max_step = max(step_bounds)
        # feature_seconds == 0 is a special case: it fires immediately, but
        # so does the step timeout, so the invariant is vacuously satisfied
        # (used in tests to exercise the ABORTED_BUDGET path). For any
        # positive value, the strict inequality is required.
        if t.feature_seconds > 0 and t.feature_seconds < max_step:
            raise ValueError(
                f"timeouts.feature_seconds ({t.feature_seconds}s) must be >= "
                f"max step timeout ({max_step}s) so a step-level "
                f"TimeoutEscalation can fire before the outer feature "
                f"budget cancels the task -- otherwise the 'finer-grained "
                f"signal never overwritten by a coarser one' guarantee "
                f"cannot hold. Adjust one or the other."
            )

    def _apply(self, data: dict) -> None:
        # The KEYS and the SECTION names are potluck's vocabulary and stay
        # here; the moves that consume them are the core's.
        overlay_keys(self, data,
                     ("profile", "interactive", "debate_enabled",
                      "human_only_paths"))
        overlay_sections(data, {
            "timeouts": self.timeouts,
            "debate": self.debate,
            "escalation": self.escalation,
            "diff": self.diff,
        })
        if "models" in data:
            self._apply_models(data["models"])

    def _apply_models(self, m: dict) -> None:
        # The ROLE names stay local -- potluck has a `reviewer`, menu has a
        # `red_team`, and a core enumerating either would know one profile
        # from another (HC4 as ruled). The field-wise merge itself is shared:
        # a partial override (e.g. a .resolved.toml table setting only `cmd`)
        # keeps the lower-precedence values for the fields it omits, honouring
        # the documented layering at the FIELD level.
        for role in ("reviewer", "tiebreaker"):
            if role in m and isinstance(m[role], dict):
                setattr(self.models, role,
                        overlay_backend(getattr(self.models, role), m[role]))

    def _apply_env(self) -> None:
        # A couple of high-value env overrides for CI.
        if os.environ.get("HARNESS_NONINTERACTIVE") == "1":
            self.interactive = False
        if os.environ.get("HARNESS_NO_DEBATE") == "1":
            self.debate_enabled = False

    def as_dict(self) -> dict:
        return asdict(self)
