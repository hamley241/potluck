"""Prompt packing + prompt-size instrumentation contracts.

Two DIFFERENT problems live under "large prompts" and get opposite treatments:

  * HARD limit -- argv-delivered backends put the whole prompt in one command
    argument, so a big diff can exceed the OS ARG_MAX. Handled by packing the
    diff to fit at file boundaries, with a visible marker for every omitted file.
  * SOFT drift -- codex loses grounding as stdin grows (docs/observations.md
    001). NOT fixed by truncation (dropping a file silently is worse than a
    drifting review); only MEASURED via prompt_size / prompt_large events.

These are deterministic harness contracts -- no real model calls. We drive the
packing helpers directly and, for the escalation path, run a full feature with a
stubbed doer/gate/diff and a real ReviewerClient bound to an argv backend.
"""

import asyncio

from harness.config import HarnessConfig, Backend
from harness.orchestrator import (
    Orchestrator, DoerClient, ReviewerClient, TiebreakerClient, GateResult,
    PromptTooLarge, call_backend,
    _assemble_prompt, _pack_diff_prompt, _split_diff_into_files,
    _derive_argv_budget, _serialized_env_bytes, _git_quote_path,
    _review_framing, _review_followup_framing, _closure_framing,
    _tiebreak_framing, _OMIT_MARKER_PREFIX, _ARGV_BUDGET_FALLBACK,
    _MAX_ARG_STRLEN,
)
from harness import orchestrator as orch
from harness.schemas import DoerResponse, IssueResponse, Outcome


SPEC = "Implement the widget."
ACC = "1. Widget exists.\n2. Widget is tested."

ARGV_BACKEND = Backend("kimi", ["kimi", "-p"], "text", stdin=False)
STDIN_BACKEND = Backend("claude", ["claude", "-p"], "text", stdin=True)


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _file_chunk(path: str, nfill: int) -> str:
    """A single-file diff chunk: `diff --git` header + one complete hunk. The
    hunk body is `nfill` filler bytes so we can size chunks precisely."""
    a, b = f"a/{path}", f"b/{path}"
    return (f"diff --git {a} {b}\n"
            f"index 0000000..1111111 100644\n"
            f"--- {a}\n+++ {b}\n"
            f"@@ -0,0 +1 @@\n"
            f"+{'x' * nfill}\n")


def _quoted_file_chunk(path: str, nfill: int) -> str:
    """A single-file diff chunk whose path needs git-quoting (space/non-ASCII),
    emitted with the same quoted `diff --git` header git itself produces so the
    existing header parser recovers the path."""
    a, b = _git_quote_path(f"a/{path}"), _git_quote_path(f"b/{path}")
    return (f"diff --git {a} {b}\n"
            f"index 0000000..1111111 100644\n"
            f"--- {a}\n+++ {b}\n"
            f"@@ -0,0 +1 @@\n"
            f"+{'x' * nfill}\n")


class _CollectLog:
    """A prompt-log sink that records (event, fields) tuples."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def by(self, name: str) -> list[dict]:
        return [f for e, f in self.events if e == name]


# --- Test 1: all FOUR diff-carrying builders pack (the whole class) ---

def test_all_four_builders_pack() -> bool:
    ok = True
    rejections = DoerResponse(responses=[
        IssueResponse(id="I1", decision="reject", reasoning="no")])
    framings = {
        "review": _review_framing(SPEC, ACC),
        "followup": _review_followup_framing(SPEC, ACC, rejections),
        "closure": _closure_framing(SPEC),
        "tiebreak": _tiebreak_framing(SPEC, ACC, "I1", "arg a", "arg b"),
    }
    # An over-budget diff of several files for every framing.
    diff = "".join(_file_chunk(f"f{i}.py", 1000) for i in range(6))
    for name, framing in framings.items():
        budget = _b(framing) + 2000  # room for a file or two, not all six
        packed = _pack_diff_prompt(framing, diff, budget)
        within = packed.final_bytes <= budget
        has_marker = _OMIT_MARKER_PREFIX in packed.prompt
        omitted = len(packed.omitted) >= 1
        good = within and has_marker and omitted
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] four_builders_pack:{name} "
              f"within={within} marker={has_marker} omitted={len(packed.omitted)}")
    return ok


# --- Test 2: whole files only -- never split inside a file ---

def test_whole_files_only() -> bool:
    files = [_file_chunk(f"f{i}.py", 400 + 100 * i) for i in range(4)]
    diff = "".join(files)
    framing = _review_framing(SPEC, ACC)
    _, parsed = _split_diff_into_files(diff)
    budget = _b(framing) + 1500  # keeps a couple, omits the rest
    packed = _pack_diff_prompt(framing, diff, budget)

    ok = True
    # Every kept file appears VERBATIM (header + all hunks); every omitted file's
    # `diff --git` header appears NOWHERE -- only as a marker.
    for path, text in parsed:
        if path in packed.kept_paths:
            present = text in packed.prompt
            if not present:
                ok = False
                print(f"  [FAIL] whole_files_only: kept {path} not verbatim")
        else:
            header = f"diff --git a/{path} b/{path}\n"
            if header in packed.prompt:
                ok = False
                print(f"  [FAIL] whole_files_only: omitted {path} header leaked")
    # Number of file headers in the packed prompt == kept count (no partials).
    header_count = packed.prompt.count("diff --git ")
    counts_match = header_count == len(packed.kept_paths)
    ok = ok and counts_match and len(packed.omitted) >= 1
    print(f"  [{'PASS' if ok else 'FAIL'}] whole_files_only "
          f"kept={len(packed.kept_paths)} headers={header_count} "
          f"omitted={len(packed.omitted)}")
    return ok


# --- Test 3: order preserved; a later small file still fits past a huge one ---

def test_order_preserved_later_small_fits() -> bool:
    small1 = _file_chunk("small1.py", 80)
    huge = _file_chunk("huge.py", 8000)
    small2 = _file_chunk("small2.py", 80)
    diff = small1 + huge + small2
    framing = _review_framing(SPEC, ACC)
    _, parsed = _split_diff_into_files(diff)
    sizes = {p: _b(t) for p, t in parsed}
    budget = _b(framing) + sizes["small1.py"] + sizes["small2.py"] + 800
    packed = _pack_diff_prompt(framing, diff, budget)

    kept = packed.kept_paths
    omitted_paths = [p for p, _ in packed.omitted]
    both_small_kept = kept == ["small1.py", "small2.py"]
    huge_omitted = omitted_paths == ["huge.py"]
    # Order preserved in the emitted prompt.
    order_ok = (packed.prompt.index("small1.py") < packed.prompt.index("small2.py"))
    within = packed.final_bytes <= budget
    ok = both_small_kept and huge_omitted and order_ok and within
    print(f"  [{'PASS' if ok else 'FAIL'}] order_preserved kept={kept} "
          f"omitted={omitted_paths} order_ok={order_ok} within={within}")
    return ok


# --- Test 3b: marker bytes are charged ONLY for omitted files ---
#
# Regression for the over-reservation bug: reserving worst-case marker space for
# EVERY file (kept ones too) dropped files a budget could actually hold.

def test_markers_charged_only_for_omitted() -> bool:
    ok = True
    framing = _review_framing(SPEC, ACC)
    files = [_file_chunk(f"f{i}.py", 500) for i in range(3)]
    diff = "".join(files)
    _, parsed = _split_diff_into_files(diff)
    sizes = [_b(t) for _, t in parsed]

    # (a) A budget that fits the WHOLE diff keeps every file -- no phantom marker
    #     reservation forces an omission.
    full = _b(framing) + sum(sizes)
    p = _pack_diff_prompt(framing, diff, full)
    keep_all = (len(p.omitted) == 0 and p.kept_paths == ["f0.py", "f1.py", "f2.py"]
                and p.prompt == framing + diff and p.final_bytes <= full)
    ok = ok and keep_all
    print(f"  [{'PASS' if keep_all else 'FAIL'}] markers_charged: full budget "
          f"keeps all kept={len(p.kept_paths)} omitted={len(p.omitted)}")

    # (b) A budget that fits two files PLUS only the one dropped file's marker
    #     keeps those two -- markers for the kept files are never charged.
    hdr = _b(orch._OMIT_BLOCK_HEADER) + 1
    marker = f"{_OMIT_MARKER_PREFIX}{_git_quote_path('f2.py')} ({sizes[2]} bytes)\n"
    budget = _b(framing) + sizes[0] + sizes[1] + hdr + _b(marker)
    p = _pack_diff_prompt(framing, diff, budget)
    two_kept = (p.kept_paths == ["f0.py", "f1.py"]
                and [o[0] for o in p.omitted] == ["f2.py"]
                and p.final_bytes <= budget)
    ok = ok and two_kept
    print(f"  [{'PASS' if two_kept else 'FAIL'}] markers_charged: two files + "
          f"one marker kept={p.kept_paths} omitted={[o[0] for o in p.omitted]}")
    return ok


# --- Test 3c: framing exactly at budget with no diff fits (no false raise) ---

def test_framing_exact_fit_no_diff() -> bool:
    ok = True
    framing = _review_framing(SPEC, ACC)

    # Exact fit, no diff content -> the prompt fits, so packing must NOT raise.
    p = _pack_diff_prompt(framing, "", budget=_b(framing))
    exact_ok = p.prompt == framing and p.omitted == [] and p.kept_paths == []
    ok = ok and exact_ok
    print(f"  [{'PASS' if exact_ok else 'FAIL'}] framing_exact_fit: exact budget "
          f"+ empty diff fits (no raise)")

    # One byte short still raises -- the boundary is strict.
    raised = False
    try:
        _pack_diff_prompt(framing, "", budget=_b(framing) - 1)
    except PromptTooLarge:
        raised = True
    ok = ok and raised
    print(f"  [{'PASS' if raised else 'FAIL'}] framing_exact_fit: one byte short "
          f"raises")
    return ok


# --- Test 4: framing alone over budget raises AND escalates no-signal ---

class _StubDoer(DoerClient):
    async def implement(self, spec, acceptance): return "done"
    async def respond_to_review(self, spec, acceptance, diff, verdict):
        return DoerResponse(responses=[])
    async def apply_fixes(self, issues, diff): return "applied"


async def _gate_ok():
    return GateResult(ok=True, output="").to_json_str()


async def _diff_source():
    return _file_chunk("app.py", 50)


async def test_framing_over_budget_raises_and_escalates() -> bool:
    ok = True
    # (a) The helper itself raises rather than emitting a mutilated prompt.
    framing = _review_framing(SPEC, ACC)
    raised = False
    try:
        _pack_diff_prompt(framing, _file_chunk("x.py", 100), budget=_b(framing) - 1)
    except PromptTooLarge:
        raised = True
    ok = ok and raised
    print(f"  [{'PASS' if raised else 'FAIL'}] framing_over_budget: helper raises")

    # (b) The caller path turns it into a clean no-signal escalation, not a
    #     crash. A huge safety margin drives the derived argv budget negative, so
    #     the framing alone cannot fit -> PromptTooLarge (a RuntimeError) flows
    #     through StepRunner into ESCALATED_NO_SIGNAL.
    cfg = HarnessConfig()
    cfg.models.reviewer = ARGV_BACKEND
    cfg.prompt.argv_safety_margin_bytes = 10 ** 9
    reviewer = ReviewerClient(cfg)
    orchestrator = Orchestrator(
        cfg, _StubDoer(), reviewer, None, _gate_ok, _diff_source)
    result = await orchestrator.run_feature(SPEC, ACC)
    escalated = result.outcome == Outcome.ESCALATED_NO_SIGNAL
    ok = ok and escalated
    print(f"  [{'PASS' if escalated else 'FAIL'}] framing_over_budget: "
          f"caller escalates -> {result.outcome.value}")
    return ok


# --- Test 5: stdin backends are NEVER packed or truncated ---

def test_stdin_never_packed() -> bool:
    framing = _review_framing(SPEC, ACC)
    diff = "".join(_file_chunk(f"f{i}.py", 5000) for i in range(20))  # ~100 KB
    log = _CollectLog()
    prompt = _assemble_prompt(
        STDIN_BACKEND, framing, diff, step="reviewer:review",
        prompt_cfg=HarnessConfig().prompt, log=log)
    # stdin is byte-identical to the legacy builders: framing + diff + "\n".
    complete = diff in prompt and prompt == framing + diff + "\n"
    no_markers = _OMIT_MARKER_PREFIX not in prompt
    no_pack_events = not log.by("prompt_packed") and not log.by("prompt_budget")
    delivery_stdin = all(f.get("delivery") == "stdin"
                         for _, f in log.events)
    ok = complete and no_markers and no_pack_events and delivery_stdin
    print(f"  [{'PASS' if ok else 'FAIL'}] stdin_never_packed complete={complete} "
          f"no_markers={no_markers} no_pack_events={no_pack_events}")
    return ok


# --- Test 6: markers are complete and git-quoted ---

def test_markers_complete_and_quoted() -> bool:
    ok = True
    # (a) A path needing git-quoting appears quote-encoded in its marker.
    weird = "my dir/café file.py"
    framing = _review_framing(SPEC, ACC)
    diff_q = _quoted_file_chunk(weird, 2000)
    packed = _pack_diff_prompt(framing, diff_q, budget=_b(framing) + 500)
    quoted = _git_quote_path(weird)
    marker_line = f"{_OMIT_MARKER_PREFIX}{quoted} ("
    quoted_ok = (marker_line in packed.prompt
                 and len(packed.omitted) == 1
                 and packed.omitted[0][0] == weird
                 # the RAW (unquoted) path must not appear as a bare marker
                 and f"{_OMIT_MARKER_PREFIX}{weird} " not in packed.prompt)
    ok = ok and quoted_ok
    print(f"  [{'PASS' if quoted_ok else 'FAIL'}] markers_quoted quoted={quoted!r}")

    # (b) The prompt_packed event's omitted list is COMPLETE (every omitted path,
    #     never truncated to a first-N). Force many files over a small budget via
    #     a stubbed budget derivation, then compare the event to a direct pack.
    diff_many = "".join(_file_chunk(f"g{i}.py", 800) for i in range(12))
    target = _b(_review_framing(SPEC, ACC)) + 5000
    orig = orch._derive_argv_budget
    orch._derive_argv_budget = (
        lambda cmd, margin, arg_max, environ: (target, "sysconf", "arg_max"))
    try:
        log = _CollectLog()
        _assemble_prompt(ARGV_BACKEND, _review_framing(SPEC, ACC), diff_many,
                         step="reviewer:review",
                         prompt_cfg=HarnessConfig().prompt, log=log)
    finally:
        orch._derive_argv_budget = orig
    direct = _pack_diff_prompt(_review_framing(SPEC, ACC), diff_many, target)
    packed_events = log.by("prompt_packed")
    complete = (len(packed_events) == 1
                and len(direct.omitted) >= 2
                and [o["path"] for o in packed_events[0]["omitted"]]
                    == [p for p, _ in direct.omitted])
    ok = ok and complete
    print(f"  [{'PASS' if complete else 'FAIL'}] markers_complete "
          f"omitted={len(direct.omitted)}")
    return ok


# --- Test 7: instrumentation logs size; prompt_large only above threshold ---

def test_instrumentation() -> bool:
    ok = True
    cfg = HarnessConfig().prompt

    # prompt_size logged for a (stdin) model call with the right delivery mode;
    # a small prompt does NOT fire prompt_large.
    small_log = _CollectLog()
    small_prompt = _assemble_prompt(
        STDIN_BACKEND, "framing ", "tiny diff", step="reviewer:review",
        prompt_cfg=cfg, log=small_log)
    size_events = small_log.by("prompt_size")
    size_ok = (len(size_events) == 1
               and size_events[0]["delivery"] == "stdin"
               and size_events[0]["bytes"] == _b(small_prompt)
               and not small_log.by("prompt_large"))
    ok = ok and size_ok
    print(f"  [{'PASS' if size_ok else 'FAIL'}] instrumentation: size logged, "
          f"no large below threshold")

    # Above the threshold prompt_large fires -- and changes NOTHING: the returned
    # prompt is still exactly framing+diff (stdin, unpacked).
    big_cfg = HarnessConfig().prompt
    big_cfg.large_prompt_warn_bytes = 10
    framing, diff = "framing ", "x" * 500
    big_log = _CollectLog()
    big_prompt = _assemble_prompt(
        STDIN_BACKEND, framing, diff, step="reviewer:review",
        prompt_cfg=big_cfg, log=big_log)
    large_events = big_log.by("prompt_large")
    large_ok = (len(large_events) == 1
                and large_events[0]["threshold"] == 10
                and large_events[0]["bytes"] == _b(big_prompt)
                and big_prompt == framing + diff + "\n")  # unpacked stdin
    ok = ok and large_ok
    print(f"  [{'PASS' if large_ok else 'FAIL'}] instrumentation: large fires "
          f"above threshold, prompt unchanged")

    # Delivery mode is reported as argv for an argv backend.
    argv_log = _CollectLog()
    _assemble_prompt(ARGV_BACKEND, _review_framing(SPEC, ACC), "diff\n",
                     step="reviewer:review", prompt_cfg=cfg, log=argv_log)
    argv_ok = argv_log.by("prompt_size")[0]["delivery"] == "argv"
    ok = ok and argv_ok
    print(f"  [{'PASS' if argv_ok else 'FAIL'}] instrumentation: argv delivery")
    return ok


# --- Test 8: budget derivation from sysconf, env, margin; logged fallback ---

def _overhead(cmd, environ, margin) -> int:
    """The non-prompt bytes the derivation reserves, mirroring the impl exactly:
    serialized env + argv (each NUL-terminated, PLUS the prompt arg's own NUL) +
    the argv/envp pointer arrays (8 bytes each, +2 NULLs) + the safety margin."""
    argv_bytes = sum(_b(a) + 1 for a in cmd) + 1
    env_bytes = _serialized_env_bytes(environ)
    pointer_bytes = 8 * ((len(cmd) + 1) + len(environ) + 2)
    return env_bytes + argv_bytes + pointer_bytes + margin


def test_budget_derivation() -> bool:
    ok = True
    cmd = ["kimi", "-p"]
    margin = 4096
    environ = {"HOME": "/home/x", "PATH": "/usr/bin:/bin"}

    # sysconf path (arg_max small enough that the TOTAL binds, not the per-arg
    # cap): subtracts env, argv (incl. the prompt NUL), pointer arrays, margin.
    budget, source, bound = _derive_argv_budget(cmd, margin, 100_000, environ)
    expected = 100_000 - _overhead(cmd, environ, margin)
    sysconf_ok = source == "sysconf" and bound == "arg_max" and budget == expected
    ok = ok and sysconf_ok
    print(f"  [{'PASS' if sysconf_ok else 'FAIL'}] budget: sysconf subtracts "
          f"env+argv+ptr+margin ({budget} == {expected}) bound={bound}")

    # A bigger environment (and bigger margin) shrink the budget -- proving both
    # are actually read. (Still small enough that the total binds.)
    budget2, _, _ = _derive_argv_budget(
        cmd, margin, 100_000, {**environ, "EXTRA": "z" * 1000})
    budget3, _, _ = _derive_argv_budget(cmd, margin + 5000, 100_000, environ)
    reads_env_margin = budget2 < budget and budget3 == budget - 5000
    ok = ok and reads_env_margin
    print(f"  [{'PASS' if reads_env_margin else 'FAIL'}] budget: env & margin "
          f"both reduce budget")

    # PER-ARG CAP BINDS. With a huge ARG_MAX the ARG_MAX-derived total is far
    # bigger than Linux's per-argument limit, so the per-arg cap (minus margin)
    # must win -- NOT the ~5 MB total. Pre-fix (no per-arg cap) this returned the
    # multi-megabyte figure and this assertion fails.
    pa_budget, pa_source, pa_bound = _derive_argv_budget(
        cmd, margin, 5_000_000, environ)
    arg_max_total = 5_000_000 - _overhead(cmd, environ, margin)
    per_arg_binds = (pa_bound == "per_arg"
                     and pa_budget == _MAX_ARG_STRLEN - margin
                     and pa_budget != arg_max_total
                     and pa_source == "sysconf")
    ok = ok and per_arg_binds
    print(f"  [{'PASS' if per_arg_binds else 'FAIL'}] budget: per-arg cap binds "
          f"({pa_budget} == {_MAX_ARG_STRLEN - margin}, not {arg_max_total})")

    # ...and the bound is LOGGED in prompt_budget so the transcript explains it.
    orig_am = orch._os_arg_max
    orch._os_arg_max = lambda: 5_000_000
    try:
        log = _CollectLog()
        _assemble_prompt(ARGV_BACKEND, _review_framing(SPEC, ACC), "d\n",
                         step="reviewer:review",
                         prompt_cfg=HarnessConfig().prompt, log=log)
    finally:
        orch._os_arg_max = orig_am
    ev = log.by("prompt_budget")
    bound_logged = len(ev) == 1 and ev[0]["bound"] == "per_arg"
    ok = ok and bound_logged
    print(f"  [{'PASS' if bound_logged else 'FAIL'}] budget: bound logged "
          f"({ev and ev[0].get('bound')})")

    # Fallback path (sysconf unavailable) uses the conservative constant AND --
    # per Member B -- subtracts the SERIALIZED ENVIRONMENT too (equally
    # measurable on this path). A large stubbed env must reduce the budget.
    fb_budget, fb_source, _ = _derive_argv_budget(cmd, margin, None, environ)
    fallback_ok = (fb_source == "fallback"
                   and fb_budget == _ARGV_BUDGET_FALLBACK
                       - _overhead(cmd, environ, margin))
    big_env = {**environ, "EXTRA": "z" * 1000}
    fb_big, _, _ = _derive_argv_budget(cmd, margin, None, big_env)
    env_subtracted = fb_big < fb_budget
    ok = ok and fallback_ok and env_subtracted
    print(f"  [{'PASS' if fallback_ok and env_subtracted else 'FAIL'}] budget: "
          f"fallback subtracts env ({fb_big} < {fb_budget})")

    # The fallback path is LOGGED (source=fallback) when sysconf returns nothing.
    orig = orch._os_arg_max
    orch._os_arg_max = lambda: None
    try:
        log = _CollectLog()
        _assemble_prompt(ARGV_BACKEND, _review_framing(SPEC, ACC), "d\n",
                         step="reviewer:review",
                         prompt_cfg=HarnessConfig().prompt, log=log)
    finally:
        orch._os_arg_max = orig
    budget_events = log.by("prompt_budget")
    logged_ok = (len(budget_events) == 1
                 and budget_events[0]["source"] == "fallback")
    ok = ok and logged_ok
    print(f"  [{'PASS' if logged_ok else 'FAIL'}] budget: fallback path logged")
    return ok


# --- Test 9: CRLF diffs parse to CLEAN paths (no trailing \r) ---

def test_crlf_paths_clean() -> bool:
    ok = True
    # A CRLF-terminated single-file diff. rstrip("\n") would leave "x.py\r";
    # rstrip("\r\n") must yield "x.py".
    crlf = ("diff --git a/x.py b/x.py\r\n"
            "index 0000000..1111111 100644\r\n"
            "--- a/x.py\r\n+++ b/x.py\r\n"
            "@@ -0,0 +1 @@\r\n"
            "+" + "z" * 2000 + "\r\n")
    _, parsed = _split_diff_into_files(crlf)
    clean_split = (len(parsed) == 1 and parsed[0][0] == "x.py"
                   and "\r" not in parsed[0][0])
    ok = ok and clean_split
    print(f"  [{'PASS' if clean_split else 'FAIL'}] crlf_paths: split path "
          f"{parsed and parsed[0][0]!r}")

    # And the omission marker renders the CLEAN path -- force omission with a
    # tiny budget and check the marker names "x.py", never "x.py\r".
    framing = _review_framing(SPEC, ACC)
    packed = _pack_diff_prompt(framing, crlf, budget=_b(framing) + 400)
    marker_clean = (len(packed.omitted) == 1 and packed.omitted[0][0] == "x.py"
                    and f"{_OMIT_MARKER_PREFIX}x.py (" in packed.prompt
                    and "x.py\r" not in packed.prompt)
    ok = ok and marker_clean
    print(f"  [{'PASS' if marker_clean else 'FAIL'}] crlf_paths: marker clean")
    return ok


# --- Test 10: a diff-of-a-diff does NOT mid-file split (regression pin) ---
#
# Hunk body lines are always prefixed with `+`, `-`, or a space, so a
# `diff --git` string INSIDE a hunk body never matches the header parser. Pin
# this: a diff whose single hunk contains BOTH an added and a context line that
# read like headers must still split into exactly ONE file.

def test_diff_of_a_diff_single_split() -> bool:
    nested = (
        "diff --git a/patch.txt b/patch.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/patch.txt\n+++ b/patch.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " diff --git a/ctx.py b/ctx.py\n"      # context line, leading space
        "-old\n"
        "+diff --git a/added.py b/added.py\n"  # added line, leading '+'
    )
    _, parsed = _split_diff_into_files(nested)
    single = len(parsed) == 1 and parsed[0][0] == "patch.txt"
    print(f"  [{'PASS' if single else 'FAIL'}] diff_of_a_diff: files="
          f"{[p for p, _ in parsed]}")
    return single


# --- Test 11: the four legacy prompt builders are GONE (no defs, no attrs) ---
#
# A trivially-true invariant that fails the moment someone reintroduces a
# monolithic prompt path that would bypass packing AND instrumentation.

def test_dead_builders_gone() -> bool:
    dead = ["_review_prompt", "_review_followup_prompt",
            "_tiebreak_prompt", "_closure_prompt"]
    present = [n for n in dead if hasattr(orch, n)]
    ok = not present
    print(f"  [{'PASS' if ok else 'FAIL'}] dead_builders_gone present={present}")
    return ok


# --- Test 12: stdin prompts are byte-identical to framing + diff + "\n" ---

def test_stdin_byte_identity() -> bool:
    ok = True
    rejections = DoerResponse(responses=[
        IssueResponse(id="I1", decision="reject", reasoning="no")])
    framings = {
        "review": _review_framing(SPEC, ACC),
        "followup": _review_followup_framing(SPEC, ACC, rejections),
        "closure": _closure_framing(SPEC),
        "tiebreak": _tiebreak_framing(SPEC, ACC, "I1", "arg a", "arg b"),
    }
    diff = _file_chunk("app.py", 300)
    for name, framing in framings.items():
        prompt = _assemble_prompt(
            STDIN_BACKEND, framing, diff, step="reviewer:review",
            prompt_cfg=HarnessConfig().prompt, log=_CollectLog())
        identical = prompt == framing + diff + "\n"
        ok = ok and identical
        print(f"  [{'PASS' if identical else 'FAIL'}] stdin_byte_identity:{name}")
    return ok


async def main() -> bool:
    print("=== prompt packing (argv hard limit) ===")
    results = [
        test_all_four_builders_pack(),
        test_whole_files_only(),
        test_order_preserved_later_small_fits(),
        test_markers_charged_only_for_omitted(),
        test_framing_exact_fit_no_diff(),
        await test_framing_over_budget_raises_and_escalates(),
        test_stdin_never_packed(),
        test_markers_complete_and_quoted(),
        test_instrumentation(),
        test_budget_derivation(),
        test_crlf_paths_clean(),
        test_diff_of_a_diff_single_split(),
        test_dead_builders_gone(),
        test_stdin_byte_identity(),
    ]
    ok = all(results)
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
