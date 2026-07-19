"""Reconstructed fixtures for the two malformed-reply shapes slice B3 measures.

READ THIS BEFORE USING THEM AS EVIDENCE. Both payloads below are
**RECONSTRUCTED** from the validation errors and escalation reasons the debate
log recorded -- they are **NOT captured verbatim**. The offending replies
themselves were discarded before slice A's payload capture existed
(`_bounded_payload` in the escalation detail, the `payload` field on
`closure_skipped`). So these are best-effort rebuilds of the SHAPE that failed,
not the bytes that failed.

That labelling is scope-of-claim applied to measurement data: the malformed-
reply before/after must say what kind of evidence sits on each side of it.
Everything here is "reconstructed, pre-capture." Everything captured from B3
onward is verbatim and lives elsewhere; captured fixtures accrue from here.

A reconstruction is not guaranteed to reproduce its original error against the
CURRENT tree -- the schema it failed may have loosened since. Where a shape no
longer reproduces, that is stated on the fixture, not hidden: a fixture that
claims to be a "validation defect" while validating cleanly today would be the
very inconsistency this file exists to prevent.

---------------------------------------------------------------------------
SHAPE 1 -- a doer-shaped object where a ReviewVerdict was required.
  Observed: 3 occurrences (reviewer boundary).
  Reconstructed from: escalation reasons naming a reviewer verdict that
    carried an IssueResponse's keys ({"id", "decision", "reasoning"}) instead
    of a verdict's {"issues": [...]}. A per-issue doer object emitted where the
    whole-verdict object belonged.
  Reconstructed defect kind: `validation`.
  CURRENT-TREE CAVEAT (divergence, reported not hidden): fed to `_parse_verdict`
    today this does NOT raise. `ReviewVerdict.issues` defaults to `[]` and extra
    keys are ignored, so the object validates to an EMPTY verdict -- i.e. a
    reviewer returning this shape reads as "no issues held" (a clean pass),
    never as a malformed reply. The reconstruction is kept as a RECORD of the
    observed shape, not as a live-reproducing validation case. The silent-empty
    behaviour is a separate latent finding, out of B3's scope.

SHAPE 2 -- a reply carrying three complete top-level JSON values.
  Observed: 1 occurrence.
  Reconstructed from: an AmbiguousJSON escalation whose `count` was 3 -- the
    core found three complete JSON values at the top level and refused to guess
    which one was the answer.
  Reconstructed defect kind: `ambiguity`.
  CURRENT-TREE STATUS: still reproduces. Fed to `_parse_verdict` it raises
    `jsonx.AmbiguousJSON` with `count == 3`.
---------------------------------------------------------------------------
"""

import json

# SHAPE 1: the doer's per-issue object ({"id", "decision", "reasoning"}) where a
# ReviewVerdict was required. Reconstructed; see the module docstring's caveat --
# this no longer raises against today's ReviewVerdict.
SHAPE_1_DOER_WHERE_VERDICT = json.dumps(
    {"id": "I1", "decision": "reject", "reasoning": "..."}
)
SHAPE_1_OCCURRENCES = 3
SHAPE_1_RECONSTRUCTED_DEFECT = "validation"
# True iff the fixture still reproduces its reconstructed defect against the
# current tree. Shape 1 does not (silent-empty verdict); the tests assert this
# honestly rather than pretending it raises.
SHAPE_1_REPRODUCES_TODAY = False

# SHAPE 2: three complete top-level JSON values in one reply -> AmbiguousJSON.
# Reconstructed to count == 3; still reproduces today.
SHAPE_2_THREE_TOP_LEVEL_VALUES = "\n".join(
    [
        json.dumps({"issues": []}),
        json.dumps({"issues": [{"id": "I1", "severity": "blocking",
                                "issue": "x", "suggested_fix": "y"}]}),
        json.dumps({"id": "I1", "sides_with": "a", "reasoning": "r"}),
    ]
)
SHAPE_2_OCCURRENCES = 1
SHAPE_2_RECONSTRUCTED_DEFECT = "ambiguity"
SHAPE_2_EXPECTED_COUNT = 3
SHAPE_2_REPRODUCES_TODAY = True
