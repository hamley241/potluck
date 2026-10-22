# resume preserves ignored files — codex findings

## Round 1 — SHIP, zero findings

Reviewed with the provenance disclosed (doer-written but committed, so no in-loop gate or
review) and pointed at three adversarial questions: whether any path can still delete an ignored
file; whether the doer's invented obstruction case strands a resume that used to work; and
whether removing the ignored-file refusal weakened a neighbouring check protecting tracked work.

Returned SHIP with no findings, and endorsed the two out-of-spec changes to `cli.py` and
`schemas.py` as consequential corrections rather than scope creep.
