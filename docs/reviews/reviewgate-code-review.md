# reviewgate slice — codex code review record

**Reviewer:** codex (OpenAI), model `gpt-5.6-sol` — independent, cross-provider,
fresh context, read-only sandbox. **Doer:** Claude (Anthropic). **Date:** 2026-07-21.

This is the review evidence the slice's receipt (`reviewgate-slice-receipt.md`)
points at. The implementation was reviewed adversarially against the ratified
Step-0 v5 spec (`reviewgate-step0-reconciliation.md`) across **five rounds**, each
closing a real defect in the security-critical hook-identity surface before it
reached any pushed repo. Final verdict: **SHIP**.

## Round history (HOLD → SHIP)

1. **HOLD** — 2 BLOCKING + 4 MAJOR + 1 MINOR: §E shape-check omitted `findings`
   (a receipt with no review evidence authorized); `_object_exists` read git errors
   as "absent" (fail-closed violation); doctor lacked §B semantics; potluck setup
   discarded the install rc; both doctors swallowed the gate rc; installer
   idempotence trusted a substring; the suite was too thin.
2. **HOLD** — 3 PARTIAL: same-repo pointer rejected only a leading `..` (mid-path
   `..` and scp forms escaped); date regex accepted `2026-99-99`; doctor/installer
   authenticated "ours" by substring, not exact match (installer could overwrite a
   foreign hook).
3. **HOLD** — 1 MAJOR: identity was text-mode, so a CRLF twin of the shim compared
   equal.
4. **HOLD** — 1 MAJOR: the identity set was state-dependent (chain variant included
   only while the chained file existed), misclassifying the installed shim and
   recursively self-chaining on reinstall.
5. **HOLD → then SHIP** — 1 MAJOR: identity ignored the executable bit (a byte-exact
   non-`+x` hook is a silently disabled gate). Fixed; re-review returned SHIP.

All four faces of one defect — resemblance → I/O layer → state-dependence → consumer
semantics — are banked in menu's observation 019.

## Final verdict (round 5), verbatim

> No BLOCKING, MAJOR, MINOR, or NIT findings. Executable-bit identity and
> restoration are correctly implemented at `harness_core/reviewgate/reviewgate.py`,
> regression-pinned at `harness_core/reviewgate/test_reviewgate.py`, and both
> vendored copies are byte-identical. Static review covered both CLIs and drift
> checks. Runtime tests could not execute because the read-only environment
> provides no writable temporary directory.
>
> **SHIP**

The builder's suite runs green where codex's sandbox could not (62/62 real-git
checks; all three repo gates exit 0).

## Provenance note

This slice is the reference case for its own mechanism: it lands **through the
gate it installs** — the receipt beside this file is the first receipt reviewgate
ever validates. The pre-push hook, installed before the landing push, authorized
that push against this SHIP receipt.
