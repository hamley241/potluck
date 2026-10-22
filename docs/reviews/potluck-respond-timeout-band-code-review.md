# doer:respond band — codex findings

## Round 1 — CHANGES, 1 MAJOR

**`timeout-band-step-universe`** — *"The invariant only checks configured map keys, so a new
unmapped generative doer step can inherit 300 seconds without failing the test."* Confirmed
against `resolve_doer_timeout`'s fallback to `model_call_seconds`. Folded: the suite now derives
the step universe from `"doer:*"` literals in non-test modules and requires each to be mapped.

## Round 2 — CHANGES, 1 MAJOR

**`timeout-band-literal-quote-gap`** — *"The step-universe scan ignores single-quoted doer
literals, allowing an unmapped fourth step to escape the timeout-band invariant."* Confirmed.
Folded: both quote styles, full identifier character class, three self-checks on the pattern.

## Round 3 — SHIP, zero findings

Clean over the folded range.

## The shape worth remembering

Both MAJORs were in the check's own definition of its universe, not in the behaviour under test,
and both passed by construction — a green run could never have surfaced either. This is the
category a cross-provider reviewer is for, and it is the second time in one day that a
"derived, not hard-coded" claim turned out to be derived in the wrong direction.
