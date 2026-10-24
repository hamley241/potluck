A1. `timeouts.doer_step_seconds["doer:respond"]` is 600 in the shipped default config.

A2. `doer:implement` and `doer:apply_fixes` are unchanged at 600.

A3. `feature_seconds` is unchanged at 36150, and `outer_min() <= feature_seconds` still holds for
    the default config. Demonstrated by computing both, not by assertion in prose.

A4. `c_max()` is unchanged by the edit — proving the outer bound was never the constraint here.

A5. A new or extended test pins that every generative doer step shares one ceiling, and NAMES any
    step that does not. The step set is derived, not a hard-coded list of today's three names; if
    "generative" cannot be derived from available data, the test says so plainly and pins what it
    can instead of implying a stronger check.

A6. That test is capable of failing: adding a doer step below the shared ceiling makes it red.

A7. The `--doer-timeout` override path, its precedence over the built-in map, and `validate()`'s
    refusal of a value that pushes `outer_min` past `feature_seconds` are all untouched and still
    covered by the existing suites.

A8. No existing test file is edited.

A9. `./.claude/verify.sh` exits 0.

---

**Superseded in part, 2026-10-23 — ruling 002 R3 (slice: the lease).** The `doer:implement` and
`feature_seconds` values stated above were facts when this slice landed and are no longer current:
`doer:implement` is now **1200s** (`apply_fixes` and `respond` stay 600), so `c_max` is **2405**,
`outer_min` and the default/personal/phi `feature_seconds` are **72150**, and the ci profile's
`feature_seconds` is **33670**. The text above is left unchanged as the record of this slice.
