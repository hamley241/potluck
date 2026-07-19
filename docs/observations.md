# Observations

Empirical notes from building potluck with potluck. The architecture bets on
"foreign models fail differently, so they catch each other's blind spots."
These entries record the times that bet paid off (or didn't) on the
construction site.

## 001 — Codex loses working-directory context on large stdin

Encountered while sending PR #2's diff (~19 KB inline) to `codex exec --json
--sandbox read-only` for review. Codex's first response cited paths under
the workspace root that didn't correspond to any actual repo, and its
second attempt narrated searching for a working copy that didn't exist —
it eventually completed the review from the patch text alone. On smaller
prompts (~4 KB, the design-plan round) the same command produced correct
review results with accurate line-number citations.

The pattern reproduces reliably: as stdin grows past a few KB of unrelated
prose (the "here's the design + here's the diff" bundle), codex's own
sense of "where am I looking?" drifts. Kimi under the same condition has
not shown this; its output stayed anchored to the code blocks in the
prompt regardless of prompt size.

Practical takeaway: when packaging a review prompt for codex, prefer
short framing + concentrated diff over a large narrative wrapper. When
context reset matters, prefer file-path claims that are self-contained
in the diff hunks themselves rather than in surrounding prose.

## 002 — Dual-model review: overlap structure

Full-repo review of potluck (30 KB source) sent to both codex and kimi.
Combined findings: **13 total**, **1 clear convergence**, **1
near-convergence**, **11 single-model catches** split roughly evenly
between codex and kimi.

Second dual review, PR #4 tier-B code (37 KB diff): **9 total**, **1
clear convergence** (merge-diff header bypass), **1 near-convergence**
(atomic-write metadata loss — codex noted the class, kimi enumerated the
mode/ACL/symlink cases), **7 single-model catches** — 2 codex-only, 5
kimi-only.

The overlap structure is the point. If foreign models mostly caught the
same bugs, the second reviewer would add little; you could ship on the
first pass. What actually happens: each reviewer catches a class the
other misses. Codex tends to flag Windows-portability, filename
injection into synthesizes streams, and platform-boundary concerns.
Kimi tends to flag async cancellation semantics, Pydantic error-boundary
lapses, and load-time type validation. On PR #4 v2, the two together
surfaced a real security bypass (merge diff headers) that a single-model
review would have shipped past.

The founding claim was "same-lineage models make correlated mistakes;
foreign models fail differently, so they catch Claude's blind spots."
These numbers are that claim showing up as measured behavior, not as
doctrine. Two data points in hand; a running log will accumulate more.

## 003 — Fixes get verified where they were applied, not where the class lives

The malformed-model-response crash was found by kimi during PR #1's review
rounds — on the doer path. The fix guarded one layer, the review verified
that layer, and the round closed. A later full-repo review found the same
bug class alive at three sibling sites (`_review`, `_review_followup`,
`_tiebreak` — fixed as run 1 of the self-hosted campaign), and then a
class-closure sweep of *that* fix found a fourth: the original doer-path
guard wrapped a can't-fail *re*-parse of already-serialized JSON in
`_doer_respond_to_review`, while the live parse one layer down in
`RealDoerClient.respond_to_review` sat exposed (run 1b). Three models
across four review rounds looked at this class and each verified the
cited site, not the class.

Practical takeaway for specs and reviews: when a finding names an
exception type escaping a boundary, enumerate the CLASS — grep for every
parse/decode/validate site on the same kind of input — then sweep for
members. Don't stop at the cited site, and don't trust that a guard
"on the doer path" guards the parse that actually runs on live output.

Second lesson, from run 1b's ending: the run escalated
`ESCALATED_NO_SIGNAL` *after* the code had survived debate (codex raised
one major, the doer rejected with reasoning, codex conceded) — because
the harness called `apply_fixes` with zero accepted issues and that
pointless model call errored. The honest-failure machinery refused to
report PASSED when its own apply step errored, exactly as designed; the
wart (skip `apply_fixes` on an empty accept list) became finding #9
rather than a silent success.

## Ship criterion for review rounds

Not "zero findings" — infinite regress. The ship criterion is
**severity trajectory**. When successive rounds fall from protocol holes
(false PASSED, uncaught tracebacks) to parser tolerance (crash on clean
input) to edge-case parsing and platform quirks, and the next full
adversarial pass returns nothing above "whitelist gap," hardening is
done. Each round is judged by its own catch: if a round still surfaces
a real security bypass (as this one did with merge headers), the round
was worth running.

## 005 — After history rewrites, verify by content, not by SHA

Merging a four-PR stack, the final verification asked `git branch --contains
<sha> | grep main` for four commit hashes and got MISSING for all four. Read
naively that says the merge silently failed — the exact alarm the check
exists to raise.

Nothing had failed. The branch had been rebased earlier in the session, so
every one of those commits had been rewritten with a new hash. The check was
asking whether four objects that no longer exist were reachable from main,
and answering correctly. Re-verified by CONTENT — `git grep` for
`_begin_scan`, `run_across_regions`, `class ScanError`, `NoApplicableRegions`,
`NON_REGIONAL` in main — all present.

**Commit identity is not survivable evidence; code presence is.** After any
history rewrite (rebase, squash, cherry-pick, amend) a SHA-based reachability
check reports on an object graph that no longer describes the work. Verify
the CLAIM ("this change is in main"), not the PROXY ("this hash is in main").

This is the same disease as observation 004's near-miss, mirrored: there, a
probe silently exercised the wrong tree and reported green; here, a probe
correctly examined objects that had ceased to exist and reported red. Both
are a check whose answer could not mean what the reader took it to mean. The
general defence is the same in both directions — make the check state what it
actually examined, and prefer evidence of the property over evidence of a
stand-in for the property.

## 006 — The human's prediction was falsified by the machinery being right

Recorded against the prediction, not the guard.

The coverage-mechanism change carried a test asserting an exact set: every
pattern is migrated, on the temporary-debt list, or non-regional. Merging a
main that had advanced independently, I predicted the guard would "loudly
catch" the debt list as stale, because main's p004 had gained an extracted
`_scan_region`.

The guard stayed green. It was right and the prediction was wrong: p004 has
`_scan_region` extracted but still hand-rolls its own region loop, so it is
genuinely not migrated to `run_across_regions` and belongs exactly where the
list puts it. Extraction made p004 EASIER to migrate; it did not migrate it.

Two things worth keeping. First, the distinction the guard drew and the human
did not — *has the shape* versus *uses the mechanism* — is the same
distinction that makes the `_is_migrated` check ast-based rather than a
substring grep, and it held here without being asked. Second, the direction
of the correction: a prediction of alarm, falsified by the machinery being
correct, is the cheap kind of wrong. Writing it down against the prediction
rather than quietly revising the story is what keeps the record usable as
evidence later.
