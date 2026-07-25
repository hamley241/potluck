"""§5 — one live-doer smoke test, potluck station (decider item 5; round-1 MINOR-7).

Both potluck suites are deliberately OFFLINE/MOCK: every doer in `test_orchestrator`
and friends is a stub, which is exactly why the first customer hit length/latency
walls the mock suite could never catch (obs-004: a green wider than its evidence).
This file is the one place a REAL doer call runs, so the seam the stubs cannot reach
-- the actual `claude` CLI latency and output shape -- gets smoke coverage.

ENV-GATED (T7): skipped (PASS, zero calls, zero cost) unless `POTLUCK_LIVE=1`, the
same opt-in isolation `test_real_gate.py` uses for its real-subprocess path. With
the flag, it makes ONE real `doer:implement` call through the REAL runner +
`RealDoerClient` path and asserts, DISTINCTLY:

  * HARD / deterministic -- the call COMPLETES WITHIN its resolved adaptive timeout
    (`StepResult.ok is True and not timed_out`, i.e. `asyncio.wait_for` did not fire
    at `resolve_doer_timeout("doer:implement")`) AND its output is a well-formed
    result (a non-empty reply the tolerant reader accepts). This is the C_max
    per-call ceiling grounded in a real call, not a stub.

  * OBSERVATIONAL only (logged, NOT asserted as a gate) -- elapsed wall-clock and
    output size / diff scope, as qualitative smoke evidence of undershoot behaviour.
    One real call cannot deterministically prove compact-draft or minimal-diff
    (round-1 MINOR-7), so this is printed, never gated.

Scope honesty (obs-025): this smoke grounds ONE real doer call's timing+shape. It
does NOT run a full multi-round debate, so it does not itself re-measure the
bounded-call count against `N_max`; that bound stays STRUCTURAL (the `outer_min =
C_max x N_max` formula + the pre-dispatch admission gate exercised by
`test_call_budget`). What §5 retires from the obs-017 declared gap is the "no live
doer evidence at all" part: C_max is now anchored to a real call.

Isolation: the `doer:implement` call runs `claude -p --permission-mode acceptEdits`,
which EDITS files, so it runs inside a throwaway temp git repo (cwd swapped for the
call, restored in `finally`) -- it never touches the real working tree.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time

from harness.config import HarnessConfig
from harness.orchestrator import BudgetedStepRunner, RealDoerClient

_LIVE_ENV = "POTLUCK_LIVE"
_STEP = "doer:implement"


def _init_repo(root: str) -> None:
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    # A base commit so `git diff` / `status` have a tree to speak about.
    with open(os.path.join(root, "README.md"), "w") as fh:
        fh.write("scratch repo for the potluck §5 live-doer smoke\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)


def _diff_scope(root: str) -> str:
    """Observational only: a one-line summary of what the doer changed."""
    stat = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                          stdout=subprocess.PIPE).stdout.decode().strip()
    changed = len([ln for ln in stat.splitlines() if ln.strip()])
    return f"{changed} path(s) touched"


async def main() -> bool:
    if not os.environ.get(_LIVE_ENV):
        print(f"  [SKIP] potluck live-doer smoke ({_STEP}) -- set {_LIVE_ENV}=1 to run")
        print("\nALL PASS")
        return True

    cfg = HarnessConfig()
    runner = BudgetedStepRunner(cfg)                 # the REAL orchestrator runner
    doer = RealDoerClient()                          # the REAL claude-CLI doer
    timeout = cfg.timeouts.resolve_doer_timeout(_STEP)
    spec = ("Create a new file named `hello.txt` in the repository root whose "
            "entire contents are exactly the single line `hello` followed by a "
            "newline. Make no other change.")
    acceptance = "A file hello.txt exists at the repo root containing `hello\\n`."

    results = {}
    prev_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        _init_repo(root)
        os.chdir(root)                               # acceptEdits edits cwd
        try:
            start = time.monotonic()
            res = await runner.run(_STEP, lambda: doer.implement(spec, acceptance),
                                   timeout)
            elapsed = time.monotonic() - start
        finally:
            os.chdir(prev_cwd)
        # HARD: completed within the resolved timeout, no CLI error.
        results["completed_within_timeout"] = (res.ok is True and not res.timed_out)
        # HARD: well-formed result -- a non-empty reply the tolerant reader accepts.
        output = res.output if isinstance(res.output, str) else ""
        results["output_well_formed"] = bool(output.strip())
        # OBSERVATIONAL (logged, never gated): timing + size + diff scope.
        print(f"  [OBS] elapsed {elapsed:.1f}s of {timeout}s budget "
              f"({100*elapsed/timeout:.0f}% of the {_STEP} ceiling)")
        print(f"  [OBS] doer reply size: {len(output)} chars")
        print(f"  [OBS] diff scope: {_diff_scope(root) if os.path.isdir(root) else 'n/a'}")
        if not res.ok:
            print(f"  [OBS] non-ok StepResult: timed_out={res.timed_out} "
                  f"error={res.error!r}")

    ok_all = True
    for name, passed in results.items():
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return ok_all


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
