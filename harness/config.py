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
    # DEFAULT per external-model call, for any doer step without its own
    # built-in entry (below) and absent a `--doer-timeout` override. Raised
    # 120 -> 300 (hardening Step-0 §1.1): the flagship doer routinely exceeds
    # 120s on real work -- the first-customer failure. Slow providers are common.
    model_call_seconds: int = 300
    # Step-adaptive ABSOLUTE per-doer-call bounds (hardening Step-0 §1.1). The
    # heavy generative doer steps -- which emit a whole code diff -- get 600s;
    # bounded responses inherit the 300s default. `resolve_doer_timeout` reads
    # this. Reviewer/tiebreak/gate steps are NOT doer steps and use their own
    # bounds. `--doer-timeout` (potluck fix only) is a UNIFORM override that wins
    # over this map outright (§1.1 precedence); it is threaded in at the call
    # site, so it is NOT stored here.
    doer_step_seconds: dict[str, int] = field(default_factory=lambda: {
        "doer:implement": 600,
        "doer:apply_fixes": 600,
        "doer:respond": 300,
    })
    # Per verification-gate run. Build/test can legitimately take a while;
    # tune per project -- this default is deliberately generous.
    gate_seconds: int = 300
    # Outer bound on a single feature across all rounds/retries. Set to the
    # computed outer_min = C_max x N_max (hardening Step-0 §1.3): with the default
    # 600s heaviest step, R=1, delay=5 -> C_max=1205, and N_max=30 -> 36150s
    # (~10 h). This is a CONSERVATIVE DOMINATING backstop, not an expected
    # runtime (a heavy real 2-round run dispatches ~11 bounded calls); the real
    # protection is the per-step timeout + the pre-dispatch call-budget admission.
    # Delegated default, disclosed in the first run's report, Goutham-overridable
    # (raise/lower alongside the call budget). validate() enforces >= outer_min.
    feature_seconds: int = 36150
    # The `--doer-timeout` UNIFORM override (potluck fix only). None = unset.
    # When set, it wins over the built-in map for EVERY doer step (§1.1). Set by
    # the CLI edge post-load; validate() is re-run afterward (§1.2) so a value
    # above feature_seconds REFUSES rather than silently breaking the finer-signal
    # guarantee (closing the pre-existing --gate-timeout bypass in the same spot).
    doer_timeout_override: int | None = None
    # Resume backoff (hardening Step-0 §2.4). A resumed step re-runs at the SAME
    # resolved adaptive timeout (NOT a doubling one), separated by a fixed
    # BACKOFF_DELAY, bounded by MAX_RESUME_RETRIES. Both the retry timeouts and
    # the delays live INSIDE the §1.1 effective-step-max closed form, so they are
    # covered by the outer-bound validation below -- retries can never silently
    # exceed the wall-clock guarantee.
    max_resume_retries: int = 1        # MAX_RESUME_RETRIES (R)
    backoff_delay_seconds: int = 5     # BACKOFF_DELAY

    def max_call_base(self) -> int:
        """The heaviest single-call timeout across ALL bounded call classes, as
        actually resolved (§1.3). Non-doer calls (reviewer/tiebreak/diff/closure)
        inherit `model_call_seconds`; the gate uses `gate_seconds`; doer steps
        resolve per `resolve_doer_timeout`. When `--doer-timeout` is set it is a
        UNIFORM override that REPLACES the built-in doer map (§1.1), so the map's
        (now dormant) values must NOT enter C_max -- only the override does (codex
        round-5 MAJOR: keeping them made validate() refuse a config that satisfies
        the real effective-timeout bound)."""
        if self.doer_timeout_override is not None:
            # Override replaces the doer map: doer steps are all = override.
            bases = (self.model_call_seconds, self.gate_seconds,
                     self.doer_timeout_override)
        else:
            bases = (self.model_call_seconds, self.gate_seconds,
                     *self.doer_step_seconds.values())
        return max(bases)

    def c_max(self) -> int:
        """The per-call ceiling that PROVABLY DOMINATES every bounded call class
        (§1.3): the heaviest base timeout, taken across `1 + MAX_RESUME_RETRIES`
        attempts separated by `MAX_RESUME_RETRIES` backoff delays. Because every
        bounded call (of any class) takes <= this, `c_max x N_max` dominates the
        true elapsed sum regardless of the exact per-class mix -- a miscounted
        class cannot break the bound (round-3/4 MAJOR)."""
        r = self.max_resume_retries
        return self.max_call_base() * (1 + r) + self.backoff_delay_seconds * r

    def resolve_doer_timeout(self, step: str) -> int:
        """The single doer-timeout resolution function (hardening Step-0 §1.1),
        total precedence, all values absolute seconds:
          1. `doer_timeout_override` (the `--doer-timeout` uniform override) wins
             for EVERY doer step when set;
          2. else the built-in `doer_step_seconds[step]` if mapped;
          3. else `model_call_seconds` (the default).
        No ratios, no post-scaling."""
        if self.doer_timeout_override is not None:
            return self.doer_timeout_override
        return self.doer_step_seconds.get(step, self.model_call_seconds)


@dataclass
class CallBudget:
    """The declared UPPER BOUND on the number of bounded calls a single run makes
    (hardening Step-0 §1.3): `N_max = fixed_call_budget + max_rounds x
    per_round_call_budget`. Generous integer caps, PINNED here and ENFORCED
    pre-dispatch (the orchestrator's BudgetedStepRunner refuses call N_max+1
    BEFORE it runs, as ESCALATED_CALL_BUDGET), so the `<= N_max` premise the outer
    bound rests on is enforced, not assumed (round-4 contract). Pinned above a
    STUB 2-round run's measured bounded-call count (T4b asserts that run stays
    `<= N_max`, so the pin is not a fiction)."""
    # fixed (non-round) bounded calls: implement, the two gates, the initial
    # get_diff + review, apply_fixes, the closure sweep + its grep, and headroom.
    # obs-017 gap (declared at the §1+§2 landing, PARTIALLY RETIRED by §5): the
    # N_max COUNT is pinned above a STUB 2-round run measured at 11 bounded calls;
    # the pins (N_max = 14 + max_rounds*8 = 30 at defaults) dominate with ~2.7x
    # margin. §5's live-doer smoke (`test_live_doer.py`, POTLUCK_LIVE=1) now grounds
    # C_max in a REAL provider call -- one live `doer:implement` completed at ~1% of
    # its 600s ceiling with a well-formed reply and a real 1-path edit, so the
    # per-call ceiling is no longer stub-only. What STAYS structural (honest scope,
    # obs-025): the `<= N_max` COUNT is still a formula + a stub assertion (T4b), NOT
    # a live full-debate call count -- one smoke call cannot exercise the multi-round
    # path.
    #   Blast radius (why the residual is safe): the pin can only OVER-refuse, never
    #   under-protect -- if a real run exceeded N_max it would hit a premature but
    #   RESUMABLE ESCALATED_CALL_BUDGET, not a silent overrun. The gap errs in the
    #   restrictive direction. T4b re-measures (under stubs) and asserts <= N_max.
    fixed_call_budget: int = 14
    # per debate round: doer respond (1), reviewer follow-up (1), and per-issue
    # tiebreaks -- covers a worst realistic round of ~5-6 deadlocked issues.
    per_round_call_budget: int = 8


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
class PromptConfig:
    """Knobs for prompt packing and prompt-size instrumentation.

    Two DIFFERENT problems live under "large prompts" and get opposite
    treatments (see README's argv seam and docs/observations.md 001):

      * The HARD limit -- argv-delivered backends (Backend.stdin=False, e.g.
        Kimi `-p <prompt>`) pass the whole prompt as a command argument, so a
        large diff can exceed the OS ARG_MAX. `argv_safety_margin_bytes` is the
        headroom subtracted from the derived ARG_MAX budget so we pack BELOW the
        real limit, not right at it. Read in orchestrator._derive_argv_budget.

      * The SOFT drift -- codex loses repo grounding as stdin grows. That gets
        MEASURED, never truncated. `large_prompt_warn_bytes` is the threshold
        above which a `prompt_large` event is logged. Read in
        orchestrator._assemble_prompt. This is instrumentation, not mitigation:
        crossing it changes no behavior, it just records the drift zone.

    Both knobs MUST actually be read (this project has a history of knobs that
    govern nothing); the tests assert the derivation and the threshold use them.
    """
    # Headroom under the derived ARG_MAX budget. ARG_MAX bounds argv+envp
    # together and the exact overhead (alignment, pointer array) is
    # platform-specific, so we keep a margin rather than pack to the byte.
    argv_safety_margin_bytes: int = 8 * 1024
    # Prompt byte size above which `prompt_large` fires. Sized at the low end of
    # observation 001's drift zone (codex drifted at ~19 KB, stayed accurate at
    # ~4 KB) so the warning brackets where grounding starts to slip.
    large_prompt_warn_bytes: int = 16 * 1024


@dataclass
class HarnessConfig:
    profile: str = "personal"
    debate_enabled: bool = True       # CI disables -> deterministic gate only
    # After a PASSED run whose change a reviewer actually judged, ask the
    # reviewer backend to name the bug CLASS this fix closed and emit grep
    # patterns for siblings of it elsewhere; the harness runs them and surfaces
    # only real matches. Advisory: it never changes the outcome. Read in
    # Orchestrator._closure_sweep (step a) -- a profile may set it False to keep
    # a run model-free, and the debate-disabled path never invokes it.
    closure_enabled: bool = True
    # Path-based routing: diffs touching these paths skip external review and
    # go human-only. Empty by default; flip on after checking with security.
    human_only_paths: list[str] = field(default_factory=list)

    timeouts: Timeouts = field(default_factory=Timeouts)
    debate: DebateConfig = field(default_factory=DebateConfig)
    call_budget: CallBudget = field(default_factory=CallBudget)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    models: Models = field(default_factory=Models)
    diff: DiffConfig = field(default_factory=DiffConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)

    def n_max(self) -> int:
        """The declared upper bound on bounded calls per run (§1.3):
        `fixed_call_budget + max_rounds x per_round_call_budget`."""
        return (self.call_budget.fixed_call_budget
                + self.debate.max_rounds * self.call_budget.per_round_call_budget)

    def outer_min(self) -> int:
        """The conservative dominating outer wall-clock (§1.3): `C_max x N_max`.
        Because every bounded call takes <= C_max and a run makes <= N_max of
        them (enforced pre-dispatch), this dominates the true elapsed sum, so
        feature_seconds >= outer_min makes the finer step/call signals always
        able to fire before the outer budget cancels the run."""
        return self.timeouts.c_max() * self.n_max()

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
        for name in ("model_call_seconds", "gate_seconds", "feature_seconds"):
            v = getattr(t, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"timeouts.{name} must be a non-negative int, got {v!r}"
                )
        # Step-adaptive per-doer-call bounds (hardening Step-0 §1.1): each must be
        # a non-negative int, and each participates in the outer-bound check below
        # so a raised step ceiling cannot silently exceed feature_seconds.
        for step, v in t.doer_step_seconds.items():
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"timeouts.doer_step_seconds[{step!r}] must be a non-negative "
                    f"int, got {v!r}"
                )
        if t.doer_timeout_override is not None and (
                not isinstance(t.doer_timeout_override, int)
                or t.doer_timeout_override < 0):
            raise ValueError(
                f"timeouts.doer_timeout_override must be a non-negative int or "
                f"None, got {t.doer_timeout_override!r}"
            )
        # Backoff knobs (§2.4) and call budgets (§1.3) participate in the outer
        # bound, so they must be sane non-negative ints too (fail closed).
        for name in ("max_resume_retries", "backoff_delay_seconds"):
            v = getattr(t, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"timeouts.{name} must be a non-negative int, got {v!r}")
        cb = self.call_budget
        if not isinstance(cb.fixed_call_budget, int) or cb.fixed_call_budget < 1:
            raise ValueError(
                f"call_budget.fixed_call_budget must be a positive int, got "
                f"{cb.fixed_call_budget!r}")
        if not isinstance(cb.per_round_call_budget, int) or cb.per_round_call_budget < 0:
            raise ValueError(
                f"call_budget.per_round_call_budget must be a non-negative int, "
                f"got {cb.per_round_call_budget!r}")
        # max_rounds bounds N_max (codex round-10 MINOR): a negative value makes
        # N_max -- and therefore outer_min -- NEGATIVE, which would sail past the
        # `feature_seconds >= outer_min` check below (any positive >= a negative)
        # and then refuse the very first call under a nonsensical budget. Refuse the
        # unrepresentable value at its source, not its symptom.
        if not isinstance(self.debate.max_rounds, int) or self.debate.max_rounds < 0:
            raise ValueError(
                f"debate.max_rounds must be a non-negative int, got "
                f"{self.debate.max_rounds!r}")
        # The --doer-timeout override, when set, is the effective bound for EVERY
        # doer step (§1.1), so it MUST participate in the outer-bound check --
        # this is what makes re-validation after the CLI override meaningful
        # (§1.2): a `--doer-timeout` above feature_seconds refuses here.
        # Outer-bound (hardening Step-0 §1.3, the §1<->§2 coupling): feature_seconds
        # must dominate `C_max x N_max`, not merely the single heaviest step. C_max
        # folds in the backoff-inclusive per-call ceiling (§1.1/§2.4) and N_max the
        # pre-dispatch call budget (§1.3), so this ONE inequality subsumes the old
        # `feature_seconds >= max_step` guarantee (C_max >= max_step, N_max >= 1)
        # AND makes the call-budget premise sound: because every bounded call is
        # <= C_max and a run makes <= N_max of them (enforced pre-dispatch by the
        # BudgetedStepRunner), the true elapsed sum is <= outer_min, so a
        # step-level OR call-budget signal can always fire before the outer budget
        # cancels the run. This is why §1 cannot land without §2's backoff: the
        # bound is defined in terms of it.
        #
        # The inequality is UNCONDITIONAL (codex round-9): there is no
        # feature_seconds == 0 escape. A zero outer bound fires the wall-clock
        # immediately, preempting every finer step-level / call-budget signal --
        # exactly what outer_min exists to prevent -- so a config that sets it is
        # refused, not exempted. (Tests exercising the ABORTED_BUDGET path set
        # feature_seconds=0 on a cfg they do NOT validate, which is unaffected.)
        outer_min = self.outer_min()
        if t.feature_seconds < outer_min:
            raise ValueError(
                f"timeouts.feature_seconds ({t.feature_seconds}s) must be >= "
                f"outer_min ({outer_min}s = C_max {t.c_max()}s x N_max "
                f"{self.n_max()}) so a step-level or call-budget escalation can "
                f"always fire before the outer feature budget cancels the run "
                f"(hardening Step-0 §1.3). Raise feature_seconds, or lower the "
                f"step ceilings / call budget / max_rounds."
            )

    def _apply(self, data: dict) -> None:
        # The KEYS and the SECTION names are potluck's vocabulary and stay
        # here; the moves that consume them are the core's. This merge is the
        # split's first unplanned test and it held: upstream's config-drift
        # work (`interactive` deleted, `closure_enabled` and the `prompt`
        # section added, `round_seconds` deleted as phantom) changed only the
        # vocabulary — every edit landed in the argument lists below, none in
        # the machinery. Recorded as evidence in AMENDMENTS A-3.
        overlay_keys(self, data,
                     ("profile", "debate_enabled", "closure_enabled",
                      "human_only_paths"))
        overlay_sections(data, {
            "timeouts": self.timeouts,
            "debate": self.debate,
            "call_budget": self.call_budget,
            "escalation": self.escalation,
            "diff": self.diff,
            "prompt": self.prompt,
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
        # One high-value env override for CI: HARNESS_NO_DEBATE.
        if os.environ.get("HARNESS_NO_DEBATE") == "1":
            self.debate_enabled = False

    def as_dict(self) -> dict:
        return asdict(self)
