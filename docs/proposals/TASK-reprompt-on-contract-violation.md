# TASK — one bounded re-prompt on a stated contract violation

**Status:** RULED 2026-07-19, trigger MET, not started. Lives in the
PROFILES' loops; touches no core surface, so it is a follow-up and not part
of the post-port review window.

## The trigger fired

Filed earlier with a second-occurrence trigger after a structurally-illegal
codex verdict escalated a cycle. Today's tally across shapes:

  * 3 codex schema confusions — a `DoerResponse`-shaped object
    (`{"id", "decision"}`) returned where a `ReviewVerdict` was required,
    all three on prompts above the large-prompt threshold;
  * 1 claude multi-value emission — a doer response carrying 3 complete
    top-level JSON values, which the new `jsonx` contract correctly refused
    to guess between.

Four occurrences, two models, two failure shapes, all caught at the parse
boundary — so the cost was reruns, not corruption. Trigger met.

## Why this and not a parser mode

The alternative — resurrect last-wins for doer responses — was ruled out on
error asymmetry, and the reasoning belongs here because it governs the
design: a raise costs a VISIBLE rerun of correct work; a wrong last-wins
selection writes wrong stances INTO THE LEDGER, silently. Visible waste
versus invisible corruption of the record is not a close trade. The parser
stays uniform; the LOOP absorbs the cost.

## The mechanism

On `AmbiguousJSON` or a schema-validation failure at any parse boundary:
**one** bounded re-prompt to the SAME model, stating the exact defect, then
escalate as before if it recurs.

The re-prompt names the violation concretely — not "try again":

    you returned 2 JSON values at offsets 0 and 14; return exactly one
    object matching this schema: {...}

This is not coercion and not guessing. The harness states the contract, the
model decides the content, one retry, then no-signal. It is potluck's
tolerant-parsing philosophy moved to the message layer: be liberal about
recoverable malformation, strict about what counts as an answer.

Bounded at ONE. A retry loop that keeps asking is a model-pressure machine,
and this project already rejects capitulation-under-pressure as a signal.

## Alongside

1. **Prompt-tail schema restatement, ALL four message types** — review,
   followup, doer response, tiebreak — not only above the size threshold:
   "Return exactly ONE JSON value matching: {...}" as the final line the
   model reads. Instruction recency degrades with distance from the tail,
   and this is nearly free everywhere.
2. **Per-message-type instrumentation** — counts for ambiguity raises AND
   re-prompt outcomes, so the next decision is data-governed rather than
   argued.

## The reopening trigger, stated now

If doer-path ambiguity persists at **≥3 occurrences after BOTH fixes land**,
with re-prompts failing to cure it, the documented-mode question reopens.
Any mode that ever arrives must be **caller-explicit** and
**schema-validated-last** — the final value must parse AND validate against
the expected schema, else raise. Never the blind `rfind` resurrected.

Expectation on the record: the trigger never fires. If it does, the evidence
will have earned the exception honestly.
