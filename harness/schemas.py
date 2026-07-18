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


class ClosurePattern(BaseModel):
    """One grep pattern the reviewer model proposes for the class-closure sweep.

    The model NEVER reports a site. It names the bug class and emits patterns;
    the harness runs them itself. `regex` is POSIX ERE, run by the harness via
    `git grep -E`. `rationale` explains why a match here would be the same bug.
    """
    regex: str
    rationale: str


class ClosureCandidate(BaseModel):
    """A single harness-verified sibling site: a real `file:line` that a
    reviewer-proposed pattern actually matched in the repo.

    HARNESS-VERIFIED, never model-supplied: every field here comes from real
    `git grep` output the harness ran, not from anything the model claimed. A
    hallucinated path the model invents cannot become a ClosureCandidate,
    because nothing the model says about locations is ever trusted or echoed --
    only lines the grep actually found reach the operator.
    """
    file: str
    line: int
    text: str
    pattern: str   # which regex matched, so the operator can judge relevance


class ClosureReport(BaseModel):
    """Result of a class-closure sweep after a run PASSED: the bug class the
    fix closed, the patterns the reviewer proposed, and the sibling sites the
    HARNESS found by running those patterns.

    `candidates` is populated by the harness from real `git grep` output;
    model-claimed locations are NEVER trusted or copied into it. The report is
    advisory only -- it is produced on an already-passed run and can never
    change the outcome.
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
