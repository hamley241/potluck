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
