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

WHICH STEPS (derived, never a hand-kept list), in TWO directions, because one
is not enough:

  * every key of `timeouts.doer_step_seconds` shares the ceiling; and
  * every `"doer:*"` step literal USED in the package's non-test modules is a key
    of that map.

The second direction is the load-bearing one and the first alone is a false
comfort: `resolve_doer_timeout` falls through to `model_call_seconds` (300) for
an UNMAPPED step, so a new generative step that is simply never added to the map
inherits 300 silently — and a check that only reads map keys cannot see it,
because the step is not a key. That is the exact drift this suite claims to
prevent, and it passed by construction until a cross-provider reviewer named it
(MAJOR, `timeout-band-step-universe`).

LIMIT OF THE SCAN, stated rather than implied: it finds STRING LITERALS in
either quote style, over the full identifier character class. A step name
assembled at runtime (`f"doer:{x}"`) still escapes it. Every doer step today is a
literal, and introducing a computed one should be a deliberate act — but this
check does not prove the absence of one. The narrower first version of this
regex is why that sentence is now specific about WHICH literals: "it finds string
literals" was true of a pattern that missed single-quoted ones, which is how a
stated limit can be accurate and still mislead.

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

AMENDED (ruling 002 R3, the lease, 2026-10-23): `doer:implement` rises to 1200
and the others stay 600 -- "the evidence covers implement only". That is the
explicit distinction the paragraph above asks for, so it is carried in the DATA
of this suite (`_RULED_ABOVE_BAND`), naming its authority: the band is pinned over
every OTHER doer step, and the exception must stay at or above it.

Runnable: `python3 -m harness.test_timeout_band`.
"""

import pathlib
import re
import sys

from harness.config import HarnessConfig

PACKAGE = pathlib.Path(__file__).resolve().parent
# BOTH quote styles and the full identifier character class. The first version
# matched only double-quoted, lowercase-snake literals — so `'doer:foo'` or
# `"doer:applyFixes"` escaped the scan entirely (codex MAJOR,
# `timeout-band-literal-quote-gap`). A scan that defines the universe must not
# be narrower than the language that writes it.
_DOER_LITERAL = re.compile(r"""['"](doer:[A-Za-z0-9_]+)['"]""")

# Doer steps a ruling explicitly lifts ABOVE the shared band, with the authority.
_RULED_ABOVE_BAND = {"doer:implement": "ruling 002 R3 (the lease)"}

results: dict[str, bool] = {}


def _off_band(steps: "dict[str, int]") -> "list[str]":
    """The steps whose ceiling differs from the modal ceiling — empty when the
    band holds. Returns NAMES so a failure says which step broke it."""
    if not steps:
        return []
    ceiling = max(steps.values())
    return sorted(name for name, secs in steps.items() if secs != ceiling)


def used_doer_steps(package: pathlib.Path) -> "set[str]":
    """Every `"doer:*"` literal appearing in the package's NON-TEST modules.

    Test modules are excluded deliberately: they name synthetic steps on purpose
    (`doer:unknown` to exercise the unmapped fallback, `doer:new_step` to prove
    this very checker can fail), and those must not be required in the map."""
    used: set[str] = set()
    for f in sorted(package.rglob("*.py")):
        if any(part.startswith(("_", "test_")) for part in f.relative_to(package).parts):
            continue
        used.update(_DOER_LITERAL.findall(f.read_text(encoding="utf-8")))
    return used


def unmapped(used: "set[str]", steps: "dict[str, int]") -> "list[str]":
    """Steps the code USES that the map does not cover — each silently inheriting
    the 300s default via resolve_doer_timeout's fallback."""
    return sorted(s for s in used if s not in steps)


def main() -> bool:
    cfg = HarnessConfig()
    steps = cfg.timeouts.doer_step_seconds
    band = {k: v for k, v in steps.items() if k not in _RULED_ABOVE_BAND}

    # --- the band holds over the SHIPPED defaults ---
    results["doer_steps_exist"] = len(band) > 0
    results["every_doer_step_shares_one_ceiling"] = not _off_band(band)
    if _off_band(band):
        print(f"  off-band steps: {', '.join(_off_band(band))}")
    results["respond_is_at_the_shared_ceiling"] = (
        band.get("doer:respond") == max(band.values()))
    # The ruled exception sits at or above the band, never below it. Fails if
    # implement is lowered beneath the shared ceiling.
    results["ruled_exceptions_not_below_band"] = all(
        steps.get(k, 0) >= max(band.values()) for k in _RULED_ABOVE_BAND)

    # --- NO used step escapes the map into the 300s fallback (codex MAJOR r1) ---
    used = used_doer_steps(PACKAGE)
    results["used_doer_steps_found"] = len(used) > 0
    missing = unmapped(used, steps)
    results["every_used_doer_step_is_mapped"] = not missing
    if missing:
        print(f"  unmapped doer steps (each silently gets "
              f"{cfg.timeouts.model_call_seconds}s): {', '.join(missing)}")

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
    # the SECOND direction can fail too: a used-but-unmapped step is named
    results["unmapped_step_is_detected"] = (
        unmapped({"doer:implement", "doer:ghost"}, {"doer:implement": 600})
        == ["doer:ghost"])
    results["fully_mapped_is_clean"] = not unmapped(
        {"doer:implement"}, {"doer:implement": 600, "doer:spare": 600})
    # the regex itself: both quote styles, and the full identifier class
    results["scan_finds_double_quoted"] = (
        _DOER_LITERAL.findall('x = "doer:alpha"') == ["doer:alpha"])
    results["scan_finds_single_quoted"] = (
        _DOER_LITERAL.findall("x = 'doer:beta'") == ["doer:beta"])
    results["scan_finds_camel_and_digits"] = (
        _DOER_LITERAL.findall('"doer:applyFixes2"') == ["doer:applyFixes2"])
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
