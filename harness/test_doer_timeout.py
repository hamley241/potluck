"""Step-adaptive doer timeouts + the --doer-timeout override (hardening Step-0 §1).

Pure config logic, no model calls. Covers the ratified tripwires:
  T6 -- resolve_doer_timeout honors the precedence: override wins for EVERY doer
        step; else the built-in step-adaptive map; else the raised default.
  T4 -- validate() (re-run after a CLI override, §1.2) refuses a doer_timeout_override
        above feature_seconds, and rejects negative bounds -- so the finer-signal
        guarantee cannot be silently broken by an override.
"""

import sys

from harness.config import HarnessConfig, Timeouts


def main() -> bool:
    results = {}

    # --- T6: resolve precedence -----------------------------------------
    t = Timeouts()
    # Default raised 120 -> 300 (§1.1): an UNMAPPED step gets the default.
    results["default_is_300"] = (t.model_call_seconds == 300
                                 and t.resolve_doer_timeout("doer:unknown") == 300)
    # Built-in step-adaptive map: heavy generative doer steps get 600.
    results["map_implement_600"] = (t.resolve_doer_timeout("doer:implement") == 600)
    results["map_apply_fixes_600"] = (t.resolve_doer_timeout("doer:apply_fixes") == 600)
    results["map_respond_300"] = (t.resolve_doer_timeout("doer:respond") == 300)

    # --doer-timeout override WINS for EVERY doer step (uniform), incl. mapped
    # ones and the default -- precedence step 1.
    t_ov = Timeouts(doer_timeout_override=450)
    results["override_wins_mapped"] = (t_ov.resolve_doer_timeout("doer:implement") == 450)
    results["override_wins_default"] = (t_ov.resolve_doer_timeout("doer:unknown") == 450)
    results["override_wins_respond"] = (t_ov.resolve_doer_timeout("doer:respond") == 450)

    # --- T4: validate refuses an override above the outer bound ----------
    cfg = HarnessConfig()
    cfg.timeouts.feature_seconds = 1200
    cfg.timeouts.doer_step_seconds = {"doer:implement": 600}
    cfg.timeouts.doer_timeout_override = 5000  # > feature_seconds
    raised = False
    try:
        cfg.validate()
    except ValueError:
        raised = True
    results["override_above_feature_refuses"] = raised

    # A within-bound override validates.
    cfg2 = HarnessConfig()
    cfg2.timeouts.feature_seconds = 1200
    cfg2.timeouts.doer_step_seconds = {"doer:implement": 600}
    cfg2.timeouts.doer_timeout_override = 900  # <= feature_seconds
    ok = True
    try:
        cfg2.validate()
    except ValueError:
        ok = False
    results["override_within_bound_ok"] = ok

    # Negative map value and negative override both refuse (fail closed).
    cfg3 = HarnessConfig()
    cfg3.timeouts.doer_step_seconds = {"doer:implement": -1}
    neg_map = False
    try:
        cfg3.validate()
    except ValueError:
        neg_map = True
    results["negative_map_refuses"] = neg_map

    cfg4 = HarnessConfig()
    cfg4.timeouts.doer_timeout_override = -1
    neg_ov = False
    try:
        cfg4.validate()
    except ValueError:
        neg_ov = True
    results["negative_override_refuses"] = neg_ov

    # The built-in step ceiling participates in the outer-bound check: a
    # feature_seconds below the 600s map ceiling refuses (the finer signal
    # could otherwise never fire).
    cfg5 = HarnessConfig()
    cfg5.timeouts.feature_seconds = 500  # < the 600 built-in implement ceiling
    step_floor = False
    try:
        cfg5.validate()
    except ValueError:
        step_floor = True
    results["step_ceiling_floors_feature"] = step_floor

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
