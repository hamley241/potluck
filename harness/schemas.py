"""Structured contracts for the debate loop.

Every cross-model interaction is forced through these schemas. The whole point
of the loop is that disagreement is about *specific, typed claims* rather than
free-form prose -- that is what lets the orchestrator detect deadlock, count
unresolved blocking issues, and decide when to escalate.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    BLOCKING = "blocking"   # must be resolved before the feature can ship
    MAJOR = "major"         # should be addressed; not a hard stop on its own
    MINOR = "minor"         # nice-to-have / style; never blocks


class ReviewIssue(BaseModel):
    """A single issue raised by a reviewer model (Codex)."""
    id: str = Field(description="stable id for this issue, e.g. 'I1'")
    severity: Severity
    issue: str = Field(description="what is wrong, concretely")
    suggested_fix: str = Field(description="the reviewer's proposed remedy")


class ReviewVerdict(BaseModel):
    """A reviewer's full verdict on a diff, reviewed against the spec."""
    issues: list[ReviewIssue] = Field(default_factory=list)

    @property
    def has_blocking_or_major(self) -> bool:
        return any(i.severity in (Severity.BLOCKING, Severity.MAJOR) for i in self.issues)

    @property
    def blocking_ids(self) -> set[str]:
        return {i.id for i in self.issues if i.severity == Severity.BLOCKING}

    @property
    def deadlock_ids(self) -> set[str]:
        """Issue ids eligible for the deadlock/tiebreak path -- blocking OR
        major. A reviewer flagged a `major` issue as materially wrong; if the
        doer rejects it, that disagreement deserves the same tiebreak as
        rejected `blocking`. `minor` never enters deadlock."""
        return {i.id for i in self.issues
                if i.severity in (Severity.BLOCKING, Severity.MAJOR)}


class IssueResponse(BaseModel):
    """The doer's (Claude's) response to a single reviewer issue."""
    id: str = Field(description="matches ReviewIssue.id")
    decision: Literal["accept", "reject"]
    reasoning: str = Field(description="why accept (with fix plan) or why reject")


class DoerResponse(BaseModel):
    """Claude's response to a full review round."""
    responses: list[IssueResponse] = Field(default_factory=list)

    def rejected_ids(self) -> set[str]:
        return {r.id for r in self.responses if r.decision == "reject"}

    def accepted_ids(self) -> set[str]:
        return {r.id for r in self.responses if r.decision == "accept"}


class TiebreakVerdict(BaseModel):
    """Kimi's adjudication on a single contested issue.

    The tiebreaker sees ARGUMENT A and ARGUMENT B with no hint of which model
    authored which (anti-brand-bias) and no fixed role→slot mapping
    (anti-position-bias -- A/B is randomized per issue by the orchestrator).
    It picks a slot; the orchestrator, which holds the mapping, translates
    that slot back to doer/reviewer.
    """
    id: str
    sides_with: Literal["a", "b", "unclear"]
    reasoning: str


class Outcome(str, Enum):
    PASSED = "passed"                       # gate green, debate resolved, fixes applied
    ESCALATED_DISAGREEMENT = "escalated_disagreement"  # unresolved blocking after cap
    ESCALATED_TIMEOUT = "escalated_timeout"            # repeated hangs
    ESCALATED_GATE = "escalated_gate"                  # gate could not be made green
    ESCALATED_NO_SIGNAL = "escalated_no_signal"        # a model call errored (auth/missing)
    ABORTED_BUDGET = "aborted_budget"                  # whole-feature wall-clock blown
    ESCALATED_CALL_BUDGET = "escalated_call_budget"    # N_max bounded-call budget exhausted (§1.3)


# --- Resumable-run checkpoint types (hardening Step-0 §2) ---------------------
#
# The whole slice serves one invariant (§0): "a run may lose a call, never its
# history." §2 makes any timeout non-catastrophic by persisting a typed execution
# cursor + completed state so the FAILED call resumes exactly. These are the
# serializable shapes; the behaviour (git-tree snapshot/restore, atomic write,
# fail-closed identity recompute) lives in harness/checkpoint.py.

# Bumped whenever the on-disk checkpoint shape changes incompatibly. Resume
# REFUSES an unknown version (fail-closed, §2.1) rather than misreading it.
# v2 (conductor §5): the identity binds `gate_binding_digest`. A v1 checkpoint
# (no gate binding) refuses on resume — the explicit version discriminator, NOT a
# missing-field fallback that would let a gate downgrade slip through (codex §5 r2).
CHECKPOINT_SCHEMA_VERSION = 2


class Phase(str, Enum):
    """potluck's STATION-SPECIFIC resumable-call phases (hardening Step-0 §2.0).

    Each phase maps 1:1 onto a real resumable model-call site in potluck's
    orchestrator, so a timeout in ANY call dispatches to the exact failed call.
    This is NOT a subset of menu's phase set (round-2 BLOCKING): `IMPLEMENT`
    (`doer:implement`) and `APPLY_FIXES` (`doer:apply_fixes`) are potluck-ONLY
    generative doer phases with no menu analogue. The set is closed; an
    unknown/absent phase on resume REFUSES (fail-closed, §2.0). `DONE` is terminal.
    """
    IMPLEMENT = "implement"          # doer:implement (orchestrator implement step)
    GATE = "gate"                    # initial verification gate
    REVIEW = "review"               # fresh reviewer pass
    ROUND_RESPOND = "round_respond"  # doer:respond within a debate round
    ROUND_FOLLOWUP = "round_followup"  # reviewer follow-up within a round
    TIEBREAK = "tiebreak"            # tiebreaker adjudication
    APPLY_FIXES = "apply_fixes"      # doer:apply_fixes (accepted-fix application)
    GATE_FINAL = "gate_final"        # closing verification gate
    DONE = "done"                    # terminal


class ExecCursor(BaseModel):
    """A persisted program counter, written BEFORE and AFTER every resumable
    model call (§2.0). Resume dispatches on `(station, phase, round_index)`, NOT
    on `rounds_used` -- `rounds_used` alone cannot resume a TAIL call (the first
    customer's actual failure: a call AFTER 9 completed rounds)."""
    station: Literal["potluck"] = "potluck"
    phase: Phase
    round_index: int = 0            # which debate round, when phase is round-scoped
    failed_step: str | None = None  # the step that timed out (for exact retry)
    attempt: int = 0                # retry attempt count for failed_step
    # The §2.0 "before AND after" program counter: a HEAD write (completed=False)
    # before a call makes a timeout IN it resume AT it; a DONE write
    # (completed=True) after it succeeds makes a crash AFTER it resume PAST it,
    # never re-running the completed call (discarding its tree/debate work) --
    # codex round-1 BLOCKING.
    completed: bool = False


class TreeSnapshot(BaseModel):
    """potluck's lossless git-tree checkpoint (§2.3, adopts Ruling A). Captures
    BOTH the working tree AND the REAL index as content-addressed git OIDs -- a
    textual `git diff` is INSUFFICIENT (binary/size guards can omit files). The
    real index is captured because restoring the working tree alone leaves the
    resumed repo's index corrupt, diverging potluck's diff/gate (round-4 MAJOR)."""
    repo_base_commit: str      # HEAD the run began from (restore resets here first)
    work_tree_oid: str         # tree OID of tracked+untracked+ignored working content
    index_tree_oid: str        # tree OID of the REAL staged index at checkpoint time
    tree_snapshot_digest: str  # canonical digest over `git ls-tree -r work_tree_oid`


class CheckpointIdentity(BaseModel):
    """The recomputed commitment set (§2.1). Resume recomputes each field from the
    run being resumed and REFUSES on ANY divergence -- a mutated criteria, a
    different input doc, a config change that would alter timeouts/caps, an unknown
    schema, or (potluck) a base/tree mismatch. run_id/directory identity is NOT the
    binding (that is circular, round-1 MAJOR-2); the binding is this recomputed set."""
    schema_version: int
    criteria_content_hash: str    # the sealed criteria
    input_document_digest: str    # the draft/doc the run began from
    effective_config_digest: str  # the resolved timeouts + caps actually in force
    run_manifest_id: str          # the run's own manifest id (bound to the input)
    gate_binding_digest: str      # the gate: an external gate's frozen-copy digest,
                                  # or the in-tree sentinel (conductor §5). Binds the
                                  # gate MODE + bytes into the tamper-evident identity so
                                  # a resume cannot downgrade to a mutable in-tree gate.
    repo_base_commit: str | None = None       # potluck only (§2.2)
    tree_snapshot_digest: str | None = None    # potluck only (§2.2)


class RunCheckpoint(BaseModel):
    """The typed, serializable checkpoint written to `checkpoint.json` in run_dir,
    LAST + temp-renamed at the completion of every resumable call (§2.1). Commits
    the cursor, completed-round count, the identity commitment set, the potluck
    tree snapshot (potluck only), and the serialized debate/ledger state needed to
    re-enter without re-litigating completed rounds."""
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    cursor: ExecCursor
    rounds_used: int
    identity: CheckpointIdentity
    # Cumulative bounded calls dispatched by this LOGICAL run so far (§1.3). Carried
    # across resumes and restored into the BudgetedStepRunner so repeated resumes
    # cannot each admit a fresh N_max and evade the total-call bound (codex round-1
    # MAJOR): the counter is per-logical-run, not per-process.
    # ge=0 (codex round-10 MAJOR): a corrupted checkpoint with a NEGATIVE count
    # would let the resumed run dispatch N_max+1 calls before refusal, breaking the
    # `<= N_max` premise under outer_min = C_max x N_max. Refuse it at parse.
    calls_dispatched: int = Field(default=0, ge=0)
    tree: TreeSnapshot | None = None            # potluck only (§2.3)
    state: dict = Field(default_factory=dict)   # serialized debate-position locals


class ClosurePattern(BaseModel):
    """One grep pattern the reviewer model proposes for the class-closure sweep.

    The model supplies patterns and explanation ONLY: no location it claims
    ever becomes a reported candidate. It names the bug class and emits
    patterns; the harness runs them itself and draws candidates exclusively
    from real `git grep` output. `regex` is POSIX ERE, run by the harness via
    `git grep -E`, and `rationale` explains why a match here would be the same
    bug -- both are UNVERIFIED model text that may contain `file:line`-shaped
    strings, and neither is ever read as a location.

    `rationale` is bounded (single line, capped with an elision marker) before
    it reaches a ClosureReport: this pattern rides FeatureResult straight into
    --json-output, so the debate-log size contract applies here too. `regex` is
    NOT bounded that way -- a truncated regex would search for something
    different; the harness rejects an over-long regex before running it instead.
    """
    regex: str
    rationale: str


class ClosureCandidate(BaseModel):
    """A single harness-verified sibling site: a real `file:line` that a
    reviewer-proposed pattern actually matched in the repo.

    HARNESS-VERIFIED, never model-supplied: `file`, `line`, and `text` all come
    from real `git grep` output the harness ran -- never from anything the model
    claimed. A `file:line`-shaped string the model invents (in its bug_class or
    a rationale) cannot become a ClosureCandidate: candidates are drawn
    exclusively from grep matches, so a claimed location is never promoted to a
    reported candidate. `pattern` is the model's regex, kept only to explain WHY
    this line matched -- it is the one model-supplied field here and is not a
    location.

    `text` is bounded at construction (capped with an elision marker) even
    though it is real grep output, not model-supplied: a single long repo line
    -- a minified bundle, a generated file, a long data literal -- would
    otherwise carry hundreds of KB into --json-output unchecked. This report
    rides FeatureResult, so the debate-log size contract applies to it too.
    """
    file: str
    line: int
    text: str
    pattern: str   # which regex matched, so the operator can judge relevance


class ClosureReport(BaseModel):
    """Result of a class-closure sweep after a run PASSED: the bug class the
    fix closed, the patterns the reviewer proposed, and the sibling sites the
    HARNESS found by running those patterns.

    The honest guarantee: `candidates` is populated exclusively from real
    `git grep` output the harness ran -- every one is harness-verified. The
    model-supplied fields -- `bug_class` and each pattern's `regex`/`rationale`
    -- are EXPLANATION, not evidence: bounded (below) but NOT verified, and NOT
    locations. A model may write a `src/ghost.py:12`-shaped string into any of
    them; that never makes it a candidate, because candidates come only from
    grep matches, never from anything the model claimed. The report is advisory
    only -- produced on an already-passed run, it can never change the outcome.

    Every free-text field carried here -- `bug_class`, each pattern's
    `rationale`, and each candidate's `text` -- is length-bounded with a visible
    elision marker before it lands. This whole report rides FeatureResult into
    --json-output, so the debate-log size contract that bounds worst-case
    FeatureResult size applies to it too; a single unbounded field would defeat
    it. (`regex` is the exception: it is rejected-not-truncated when over-long,
    since a shortened regex would silently search for something else.)
    """
    bug_class: str                     # one line: the class this fix closed
    patterns: list[ClosurePattern] = Field(default_factory=list)
    candidates: list[ClosureCandidate] = Field(default_factory=list)


class StepResult(BaseModel):
    """Uniform result for any bounded step (model call, gate run).

    A timeout is just a failure with a recovery hint -- it flows through the
    same contract as any other failure, never a special path.

    `output` is always a `str`. Callers that need structured payloads (the
    gate returns ok+output) serialize to a string at the callable boundary
    and deserialize at the consumer -- keeping StepResult.output typed means
    a model backend that accidentally returns bytes/dict is rejected at the
    boundary rather than crashing deep in _parse_verdict/_extract_json.
    """
    ok: bool
    timed_out: bool = False
    output: str = ""
    error: str | None = None
    recovery_hint: str | None = None
