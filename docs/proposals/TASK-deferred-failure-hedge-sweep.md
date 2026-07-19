# TASK — sweep the deferred-failure-hedge shape across all three repos

**Status:** SWEPT 2026-07-19, slice B3. The tenth sibling sweep, and the
second aimed at a STRUCTURAL shape rather than a bug's twin. **Result: one
instance (`_note_kw`, deleted this slice); no others.**

**Origin:** observations 012 and 013. `_note_kw()` omitted the `retry_note`
kwarg when it was `None`, so a client predating the param worked on every
normal call and raised an uncaught `TypeError` only on the RETRY — the rarest
path, entered only when a parse had already failed. The hedge converted a
loud, immediate, every-call failure into a latent one on the recovery path.

## The rule (observation 013)

> **Any compatibility shim that succeeds on the common path and fails on the
> error path has hidden the incompatibility in the single hardest place to
> debug — the moment something else has already gone wrong.**

Greps for the SHAPE, not the symbol.

## The enumeration (produced mechanically, then ruled)

Four shapes, swept across `potluck/harness`, `harness_core`, `menu/harness`
(non-test code). Each hit ruled with a one-line reason.

### Shape A — optional kwarg threaded only on a fallback/retry/error branch
Grep: `retry_note`, `_note_kw`, and the conditional-kwarg-dict idiom
`{...} if ... else {}` spread into a call.

  * `potluck` `_note_kw` — **GENUINE INSTANCE. Deleted this slice.** It was the
    only conditional-kwarg-dict spread in any of the three repos; `retry_note`
    now rides every call unconditionally at all five sites.
  * No other `{...} if ... else {}` kwarg spread exists in any repo. **None.**

### Shape B — `**kwargs` spreads that differ between the happy and error paths
Grep: `**kw`, `**kwargs`, `**fields`, `**_...` at call sites.

  * `potluck` `_log`/`_pack_event(**kw)` — not an instance: uniform on every
    path, no branch varies the spread. Already the ESTABLISHED shape.
  * `potluck` `_on_malformed` → `_log("closure_skipped", **fields)` — not an
    instance: `fields` gains `ambiguous_count`/`ambiguous_offsets` only when the
    defect is AmbiguousJSON, but that is ADDITIVE logging through the same
    `_log`, not a call whose interface narrows on error. No hidden mismatch.
  * `menu` `_set_entry(..., **kwargs)` → `LedgerEntry(..., **kwargs)` — not an
    instance: uniform passthrough; `origin` is keyword-only and named at every
    write site (its docstring pins this). Same spread on all paths.
  * `menu` `_pack_event(event, event_version, **kw)` — not an instance: the
    ledger's copy of the same uniform log helper.

### Shape C — `getattr`/`hasattr` guards covering only the common call
Grep: `getattr(`, `hasattr(`.

  * `potluck`/`menu` `config.py` reflective field overlay (`getattr(t, name)`,
    `overlay_backend(getattr(...))`, `getattr(self, field)`) — not an instance:
    reflective field copy, runs identically on every path, narrows nothing on
    error.
  * `harness_core` `config.py:136` `if hasattr(obj, k)` — not an instance: the
    same reflective overlay, common path only, no error branch.
  * `potluck` `cli.py` `hasattr(args, ...)` / `getattr(args, ..., default)` —
    not an instance: argparse-Namespace optional-arg guards; they fire on every
    invocation for optional CLI flags, not during error recovery.
  * `harness_core` `probe_backend_profiles.py` `getattr`/`hasattr` — not an
    instance: a health-probe reading backend attributes; a probe, not a shim.

### Shape D — try/except that silently narrows the interface used
Grep: `except TypeError`, `except AttributeError`, `except NotImplementedError`,
`inspect.signature`, `unexpected keyword`.

  * **None found in any repo.** No call is wrapped in a try/except that falls
    back to a narrower interface. This is the purest form of the hedge, and it
    is absent.

## Verdict

The sweep LOOKED (four shapes × three repos, non-test) and found exactly one
instance — `_note_kw`, removed this slice. No sibling of the shape survives in
`potluck`, `harness_core`, or `menu`. Per scope-of-claim, a sweep that finds
nothing beyond the origin must say so: it did, and this is that statement.

## Note — one adjacent latent finding, out of B3's scope

While reconstructing the Item 4 fixtures, a reviewer returning a doer-shaped
`{"id","decision","reasoning"}` where a `ReviewVerdict` is required does NOT
escalate today: `ReviewVerdict.issues` defaults to `[]` and extra keys are
ignored, so the reply validates to an EMPTY verdict — read as "no issues held",
a clean pass. That is a silent-narrowing of a DIFFERENT kind (a schema that
accepts too much), not the deferred-failure hedge. Recorded here, not fixed;
filed as a candidate for the next time verdict parsing is touched.
