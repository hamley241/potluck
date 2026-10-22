"""The doer timeout BAND: every generative doer step shares one ceiling, and the
shipped defaults can never be the thing that makes a run unvalidatable.

Why this exists: `doer:respond` shipped at 300s while `doer:implement` and
`doer:apply_fixes` had 600 — a split justified nowhere in the config. respond was
one of the two steps that timed out in the first-customer report (the single
biggest friction, systemic at n=2); the hardening slice then ratified a 300-600
band and left that casualty at the BOTTOM of it; and a sibling session hit the
same timeout on a real converge run (n=3). The constant is now 600. A bare
constant edit re-opens the moment a fourth doer step is added at a number picked
by feel, so the invariant is pinned here instead.

WHICH STEPS (derived, never a hand-kept list): every key of
`timeouts.doer_step_seconds`. The map IS the set of doer steps by construction —
`step_seconds()` consults it by key — so a new generative step arrives as a new
key and is governed automatically. A hand-kept list of today's three names is
exactly what lets a fourth slip in unnoticed.

WHAT "GENERATIVE" MEANS HERE, honestly: it is NOT derivable from the data. The
map carries names and integers; nothing in it marks a step as generative. Every
key present today is a model-writing call, so "all doer steps" and "all
generative doer steps" currently coincide, and this suite pins the former. If a
non-generative doer step is ever added that genuinely warrants a lower ceiling,
this test SHOULD go red and force the distinction to be made explicit in the
data rather than assumed in prose — that is the intended behaviour, not a defect.

REFINEMENT DECLINED FOR NOW, recorded so it is a choice and not an oversight: the
cross-provider panel that ruled this reclassification suggested pinning only the
steps EXPLICITLY classified heavy, so a future light step is not forced to 600.
That is the better shape, and it needs the classification carried in the DATA
(the map holds names and integers today). Until a genuinely light doer step
exists there is nothing to classify, and the going-red behaviour above is the
cheaper way to force the distinction when one appears.

WHAT THIS DOES NOT PROVE: that 600 is the right number. It proves the steps agree
and that the defaults satisfy the outer-bound guarantee; whether the band is wide
enough is an empirical question the runs answer.

HOW TO MAKE IT RED: add a key to `doer_step_seconds` with a different value, or
raise one step above the others. The self-checks below do exactly that against
synthetic configs and require the checker to fail them.

Runnable: `python3 -m harness.test_timeout_band`.
"""

import sys

from harness.config import HarnessConfig

results: dict[str, bool] = {}


def _off_band(steps: "dict[str, int]") -> "list[str]":
    """The steps whose ceiling differs from the modal ceiling — empty when the
    band holds. Returns NAMES so a failure says which step broke it."""
    if not steps:
        return []
    ceiling = max(steps.values())
    return sorted(name for name, secs in steps.items() if secs != ceiling)


def main() -> bool:
    cfg = HarnessConfig()
    steps = cfg.timeouts.doer_step_seconds

    # --- the band holds over the SHIPPED defaults ---
    results["doer_steps_exist"] = len(steps) > 0
    results["every_doer_step_shares_one_ceiling"] = not _off_band(steps)
    if _off_band(steps):
        print(f"  off-band steps: {', '.join(_off_band(steps))}")
    results["respond_is_at_the_shared_ceiling"] = (
        steps.get("doer:respond") == max(steps.values()))

    # --- the outer-bound guarantee holds for the shipped defaults (§1.3) ---
    # feature_seconds >= outer_min is what keeps the finer step/call signals able
    # to fire before the outer wall clock cancels the run. If the SHIPPED config
    # ever violated it, every run would start from an unvalidatable baseline.
    results["defaults_satisfy_outer_bound"] = (
        cfg.outer_min() <= cfg.timeouts.feature_seconds)
    results["defaults_validate"] = _validates(cfg)

    # --- raising respond did NOT move the outer bound: c_max follows the
    #     HEAVIEST step, which was already 600, so the bound was never the
    #     constraint on this change. Computed here rather than asserted in prose.
    lowered = HarnessConfig()
    lowered.timeouts.doer_step_seconds["doer:respond"] = 300
    results["c_max_unchanged_by_the_raise"] = (
        cfg.timeouts.c_max() == lowered.timeouts.c_max())
    results["outer_min_unchanged_by_the_raise"] = (
        cfg.outer_min() == lowered.outer_min())

    # --- the checker can FAIL: a step added below the ceiling is named ---
    results["off_band_step_is_detected"] = (
        _off_band({"doer:implement": 600, "doer:new_step": 300})
        == ["doer:new_step"])
    results["off_band_names_every_offender"] = (
        _off_band({"a": 600, "b": 300, "c": 120}) == ["b", "c"])
    results["uniform_band_is_clean"] = not _off_band({"a": 600, "b": 600})
    results["empty_map_is_not_a_violation"] = not _off_band({})

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


def _validates(cfg: HarnessConfig) -> bool:
    try:
        cfg.validate()
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
