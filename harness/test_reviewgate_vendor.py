"""Drift check (reviewgate currency): this repo's vendored reviewgate copy MUST
be byte-identical to the canonical source in the harness_core repo. The git
pre-push hook runs the vendored copy pre-venv; this test runs IN the venv and is
the consumer-side currency mechanism — a canonical bump reddens the consumer here
(eventual, at the next suite run). Never edit the vendored copy: change
harness_core/reviewgate/reviewgate.py and re-vendor.

The canonical lives OUTSIDE harness_core's importable engine package (in the
repo's top-level reviewgate/ dir), so it cannot be `import`ed. We derive its path
mechanically from the editable install: `harness_core.__file__` points at the
real source at `<hc-repo>/harness_core/__init__.py`, so the repo root is two
parents up, and the canonical is `<hc-repo>/reviewgate/reviewgate.py`.
"""
from __future__ import annotations

import pathlib
import sys

import harness_core

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))


def _canonical_path() -> pathlib.Path:
    hc_repo = pathlib.Path(harness_core.__file__).resolve().parent.parent
    return hc_repo / "reviewgate" / "reviewgate.py"


def test_vendored_matches_canonical():
    vendored = pathlib.Path(__file__).parent / "_vendor" / "reviewgate.py"
    canon = _canonical_path()
    check("vendored_copy_exists", vendored.exists(), str(vendored))
    check("canonical_source_found", canon.exists(),
          "canonical not found at %s (harness_core editable install expected)" % canon)
    if vendored.exists() and canon.exists():
        check("vendored_byte_identical_to_canonical",
              vendored.read_bytes() == canon.read_bytes(),
              "vendored reviewgate has DRIFTED from the harness_core canonical — "
              "re-vendor (never edit the copy in place)")


def _run_all():
    test_vendored_matches_canonical()
    failed = sum(1 for _, ok, _ in _results if not ok)
    for name, ok, detail in _results:
        mark = "PASS" if ok else "FAIL"
        print("  [%s] %-38s%s" % (mark, name, ("  " + detail) if detail and not ok else ""))
    total = len(_results)
    print("\n%s  (%d/%d)" % ("ALL PASS" if not failed else "SOME FAILED", total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
