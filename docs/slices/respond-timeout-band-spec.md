# `doer:respond` sits at the bottom of the band it should top

`timeouts.doer_step_seconds` gives `doer:implement` and `doer:apply_fixes` 600s, and
`doer:respond` 300s. All three are GENERATIVE doer calls — the model writes a substantial reply
in each. The split is not justified anywhere in the config, and the evidence runs against it.

**`doer:respond` is the step with the worst failure record in this fleet.** The first-customer
report named the doer timeout as "the single biggest friction", systemic at n=2, and the two
stations that failed failed at menu converge `doer:respond` and potluck fix `doer implement` —
so `respond` was one of the two original casualties. The hardening slice then raised the band to
300–600 and left `respond` at the BOTTOM of it. A sibling session hit the same timeout on a real
converge run today and cleared it with a run-local profile raising it to 600. That is n=3 for
this one step.

Raise `doer:respond` to 600, matching its generative siblings.

## This costs nothing in the outer bound — verify it rather than assuming

`c_max()` is driven by the HEAVIEST step, which is already 600. Computed on the current config:
`c_max` 1205 and `outer_min` 36150 both before and after, against `feature_seconds` 36150. The
change does not move the outer wall clock, does not narrow the finer-signal guarantee, and
cannot push `outer_min` past `feature_seconds`. Re-derive this in the work rather than trusting
the sentence.

## The durable half

A bare constant edit re-opens the moment someone adds a fourth doer step at a number they picked
by feel. Add a test pinning the invariants that make the band meaningful:

* every GENERATIVE doer step shares one ceiling — a new step added below it is drift, and the
  test names the offending step;
* `outer_min() <= feature_seconds` holds for the shipped default config, so the shipped defaults
  can never be the thing that makes a run unvalidatable.

Derive the step set rather than hard-coding today's three names: a hand-kept list is exactly what
lets a fourth step slip in unnoticed. If "generative" cannot be derived from the data available,
say so in the test and pin what CAN be derived, rather than pretending to a check you are not
making.

## Hard constraints

1. Only the default changes. Do not touch the `--doer-timeout` override path, the precedence
   rules, or `validate()`'s refusal behaviour.
2. Do not raise `feature_seconds`. If the change required raising it, that would be evidence the
   change is wrong.
3. The existing config-hardening suites stay green and unedited.
4. `.claude/verify.sh` stays green.

---

**Superseded in part, 2026-10-23 — ruling 002 R3 (slice: the lease).** The `doer:implement` and
`feature_seconds` values stated above were facts when this slice landed and are no longer current:
`doer:implement` is now **1200s** (`apply_fixes` and `respond` stay 600), so `c_max` is **2405**,
`outer_min` and the default/personal/phi `feature_seconds` are **72150**, and the ci profile's
`feature_seconds` is **33670**. The text above is left unchanged as the record of this slice.
