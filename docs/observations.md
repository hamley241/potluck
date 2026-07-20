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

## 007 — A union that keeps both sides can still be inconsistent

Sibling to menu's DRIFT #21, and its complement. #21's lesson was that a
more general-LOOKING fix is not a superset, so a merge that takes one side
wholesale can silently delete the other's coverage. This is the same seam
from the other direction: a merge that keeps BOTH sides can still produce
code that cannot run.

`real_run_gate` was migrated to `run_subprocess_result`, which returns
`(returncode, stdout, stderr)` and has no `proc`. Independently, drift #13's
fix added `exit_code=proc.returncode` back when the function still spawned a
bare `proc`. The merge preserved the new call AND the old reference. Git had
nothing to complain about — the two edits touch different lines — so the
conflict resolution was clean and the result raised `NameError` on every
call.

**Conflict resolution proves TEXTUAL compatibility. Only EXECUTION proves
SEMANTIC compatibility.** The variable one side named is the variable the
other side deleted, and no amount of reading the diff surfaces that as
reliably as running the function once does.

**Operational rule: after any merge that touches a function's BODY, execute
that function at least once before the merge is trusted.** Not the suite —
the function. Here the suite was green and stayed green, because of
observation 008.

## 008 — The stub seam is a structural blind spot, not an oversight

`real_run_gate` shipped broken to origin with a fully green suite because it
had ZERO executions across the entire test suite. Every orchestrator test
injects a stub gate callable — which is the project's core quality bet, the
thing that makes the whole control loop testable with no model calls and no
network, and it is correct.

But it has a complement nobody had named: **every `real_*` boundary function
sits on the far side of the stub seam, and the architecture guarantees the
suite never runs it.** The blind spot is not an oversight in any one test; it
is the shape of the strategy. Stubs prove the ORCHESTRATOR's branching.
Nothing was proving the functions that touch the real world.

That is observation 004's disease — a green wider than its evidence — in the
live system rather than in a check, sitting exactly where the architecture
put it.

The fix is not fewer stubs. It is a second, smaller family of tests that runs
the real boundary with no mocks: `harness/test_real_gate.py` drives actual
bash scripts through the actual subprocess path across all four outcome
classes (pass, ran-and-failed, could-not-run at exit 2, signal-killed with a
negative code). Nine assertions, no mocks, and it fails against the broken
form with the production error.

**Generalised as a standing requirement (ruling, 2026-07-19): enumerate the
stub seam across all three repos — every `real_*` function and every real
client — and require each to carry at least one no-mock execution test
covering its outcome classes.** Where a boundary genuinely cannot run
without a model, the testable PARTS still get executed (argv construction,
output parsing, file I/O) and the untestable remainder gets NAMED in the
test — scope-of-claim applied to the suite's own architecture. Tracked in
`docs/proposals/TASK-stub-seam-sweep.md`.

## 009 — a preservation spec is a pointer to the tree, not an authority over it

**Occurred:** 2026-07-19, slice B1 of the re-prompt task.

A spec required the tiebreak boundary to "preserve exactly" the prefix
`"malformed tiebreak verdict: "`. That is **menu's** string. potluck's is
`"tiebreaker returned malformed response: "`. The reviewer read the spec
literally and raised a blocking finding that the code deviated from it. The
doer preserved the tree's actual string and rejected the finding.

The doer was right, and the rule it applied is now law:

> **When a spec's preservation claim and the tree disagree, the tree wins
> and the spec is the defect.**

A preservation obligation *quotes nothing* — it delegates. "Preserve
exactly" is a pointer to the tree; a spec author who writes a literal beside
it has copied a value that the pointer already names, and a copy can be
stale or, as here, from the wrong repo.

**Operational rule, adopted:** preservation obligations are **read out of
the tree at spec-writing time, never recalled.** Quoting from memory is
transcription, which is the failure the handoff extractor exists to prevent;
this is that lesson applied to specs rather than to rulings.

**Filed as a candidate, NOT built (one occurrence):** a spec-lint. Any
`preserve exactly: "<string>"` clause is mechanically checkable against the
tree the moment the spec is written, before a reviewer ever sees it.
*Trigger: a second occurrence of a spec literal diverging from the tree.*

## 010 — the sibling-sweep habit is also a contamination vector

**Occurred:** same incident as 009; this is its cause, not its shape.

The wrong string was in mind because menu's orchestrator had been read an
hour earlier, chasing a suspected `RecursionError` divergence. The repos are
deliberately kept comparable so that a finding in one can be swept into the
other — eight consecutive sibling confirmations came from exactly that
habit.

**The discipline's strength is the source of its characteristic error.** The
same comparability that makes a sweep productive makes the two repos'
strings interchangeable in an author's head. This belongs beside the
author-blindness entries because it is the same genus: a property of the
worker, not of the code.

**It predicts where it recurs:** anywhere one sibling has recently been read
and the other is being written about. That is a narrow, checkable window,
which is what makes the mechanical mitigation (read, don't recall) cheap
enough to always apply.

## 011 — a reviewer assuming ecosystem convention over repo convention

Same run. The reviewer raised a major finding that the new suite was a
`main()` script rather than pytest-discovered cases, and so "contributes
zero tests." potluck's `./test` states **"No pytest"** in its header, runs
each `harness/test_*.py` as a module, and auto-discovers via `find`. The
`__main__` guard IS the convention; every existing suite has it.

The finding was rejected, and the adjudication method is the settled
pattern: **the referee reads the evidence, not the arguments** — the suite
was run directly (28 assertions, five boundaries) rather than either party
being believed.

**Cheap fix, when the review prompts are next touched:** include the repo's
test-runner convention in the reviewer's context. One line prevents a whole
class of discovery-assumption findings.

## 012 — wrong-concession case #1: the debate settled a finding incorrectly

**Occurred:** 2026-07-19, slice B2. **This is a first.** Every prior entry
records the debate MISSING something. This one records it reaching a wrong
outcome that survived to commit.

Codex raised I2 (major): the wrappers use `_note_kw()` to omit `retry_note`
entirely when it is `None`, rather than passing it unconditionally as the
spec's `call(retry_note)` contract required. The doer rejected it with a
rationale making TWO empirical claims:

  1. omitting keeps the first-attempt prompt byte-identical to today's;
  2. stubs predating the param keep working.

Codex conceded. The run PASSED. **Both claims fail when tested:**

  1. `_with_retry_note(tail, None) == tail` — the prompt is byte-identical
     whether the kwarg is omitted OR passed as `None`. The hedge buys
     nothing it claims to buy.
  2. A client lacking the param raises `TypeError: unexpected keyword
     argument 'retry_note'` on the RETRY. `TypeError` is outside the caught
     set, so it escapes as a traceback instead of escalating.

Nobody in-loop ran either claim. Post-loop review did.

**What this adds to observation 001's ledger:** fresh-context review does
not only catch what the debate *missed* — it catches what the debate
*wrongly settled*. In the record, a concession to a confident-but-wrong
rebuttal is INDISTINGUISHABLE from a concession to a correct one. The only
thing that separates them is someone testing the rebuttal's claims.

**Filed as a candidate, NOT built:** when a rejection rationale makes
empirical claims, adjudication should be RUNNING them, not weighing them —
menu's concession-reason audit discipline pointed at potluck's own loop.
*Trigger: next time the orchestrator's debate handling is touched.*

## 013 — the class: a hedge that defers a failure to the recovery path

The general shape behind 012's defect, entering the standing vocabulary:

> **Any compatibility shim that succeeds on the common path and fails on
> the error path has hidden the incompatibility in the single hardest place
> to debug — the moment something else has already gone wrong.**

`_note_kw()` is the instance: legacy clients work on every normal call and
crash only during error recovery. A shim that failed on call one would have
been found by the first test run.

The sibling sweep (the tenth) greps for the shape, not the symbol: optional
kwargs threaded only on a fallback path, `**kwargs` spreads that vary by
branch, `getattr(x, "f", None)` guards that only cover the happy call.

## 014 — the hedge closed; the sweep swept clean; two divergences reported

**Occurred:** 2026-07-19, slice B3.

`_note_kw` is deleted; `retry_note` rides all five call sites unconditionally.
The proof that this was the right shape is a test (`test_reprompt` t8): a client
whose method predates the param now fails with a loud `TypeError` on the FIRST
call — even when it would have returned a valid reply — instead of the silent
success or deferred retry-path crash the hedge produced. The failure moved from
the hardest place to debug (error recovery) to the easiest (call one).

The tenth sibling sweep (`TASK-deferred-failure-hedge-sweep`) ran the four
shapes across all three repos and found **no other instance**. `_note_kw` was
the only conditional-kwarg-dict spread anywhere; no try/except interface-narrow
exists at all. Per scope-of-claim, a sweep that finds nothing must say it
looked — it did, and the record says so.

**Two divergences surfaced, reported not edited (observation 009):**

  1. The spec said "no in-repo client changes." True of the PRODUCTION clients
     (all six base-class methods declare `retry_note`), but the test STUBS did
     not — several mock methods predated the param and, once the kwarg rode
     every call, failed on call one exactly as t8 predicts. The fix was to make
     the mocks conform to the interface they mock, not to weaken the change.
     The spec's claim was scoped too narrowly; the tree corrected it.
  2. The Item 4 fixture "a doer-shaped object where a ReviewVerdict is required"
     was reconstructed as a VALIDATION defect, but does not reproduce against
     today's tree: `ReviewVerdict` ignores extra keys and defaults `issues=[]`,
     so the reply validates to an empty verdict — a clean pass, not an error.
     The fixture is labelled with this caveat rather than faked into raising.
     The silent-empty behaviour is an adjacent latent finding, filed not fixed.

**What this adds:** the measuring device now exists. Per-message-type counters
(malformed-seen split by defect kind, re-prompts issued, cured vs recurred) and
one `reprompt` log event per retry — routed through the established
`_pack_event`/`_truncate_walk` envelope, changing no control flow. The next
malformed-reply decision can be shown from the log, not argued.

## 014 — evidence precedes rejection (PROMOTED: candidate → canon)

**Promoted 2026-07-19** on two firings of its stated trigger, one slice
apart. Filed as a candidate in 012; met in 012 (`_note_kw`'s two false
claims) and again in 013's successor slice B3 (the counters-cannot-drift
claim, false on the retry-timeout path). Both times the loop settled a
finding on an empirical claim NOBODY RAN.

The rule is the sibling of *evidence precedes act*:

> **Evidence precedes rejection.** A rejection whose rationale makes a
> testable claim attaches the test — the command and its output — or the
> claim does not qualify as a rationale.
>
> **A reviewer may not concede to an unevidenced empirical claim.** The
> legal moves are REQUEST THE EVIDENCE or HOLD. A hold routes to
> adjudication, where someone runs it.

**What it does structurally:** it converts "who tests the rebuttal?" from a
vigilance question into a protocol answer. The concession itself is gated on
evidence, so nobody has to notice the pattern in the moment — which is the
only reliable form, because both wrong concessions were made by participants
who had every reason to be careful and were not being careless.

Two wrong concessions in two consecutive slices also updates a prior: **the
debate's concede step is its softest joint.** Round-one findings get
scrutiny; concessions get waved through. The new rule hardens exactly that
joint.

## 015 — every instrumentation family ships a conservation invariant

The general form behind B3b's cure.

> **Every instrumentation family ships with its conservation invariant, or
> states why none exists.**

B3's instance: `reprompts_issued == cured + recurred + timed_out + not_ok`.
Every issued retry is an OBLIGATION that must terminate in a counted
outcome. An unaccounted retry is a lost concern wearing a different costume
— the fifth guarantee's shape one level down, and the ledger-accounting
discipline applied to a measuring device.

A measuring device without a conservation law has blind spots exactly where
reconciliation fails, and B3 proved where that real estate is: the rarest,
most error-adjacent path. The worst possible place for a gap.

**The meta-irony, recorded because it is the point:** the slice whose entire
purpose was measurement shipped the unaccountable path, and it was caught by
MEASURING THE MEASUREMENT — running the counters against the stream on a
path the tests did not cover.

## 016 — the sweep reflex applied to a FEATURE (a ruling reversed)

**Occurred:** 2026-07-20. A ruling ordered menu's re-prompt port next. It
was reversed the same session, on evidence, by the brief owner.

**The distinction that was missed:**

> **Sibling sweeps are for DEFECT CLASSES.** The same architectural decision
> made in three places means the blind spot exists everywhere *by
> construction* — so the sweep is justified before any evidence arrives in
> the sibling. **A FEATURE follows evidence.** Its trigger must fire in the
> repo that receives it.

potluck's re-prompt earned its place with four banked occurrences across two
models. menu's boundary **already escalates cleanly** (DRIFT.md #14 records
malformed-JSON handling closed upstream) and has two log hits against an
unmet threshold. The port was symmetry-driven.

**The cause is observation 010 operating one level up.** The comparability
that makes menu and potluck sweepable is the same comparability that makes a
feature look like a class. Named at the code level two slices earlier, it
recurred at the PLANNING level, in the ruling seat — which is the evidence
that 010 is a property of the work, not of any one worker.

**Sequence corrected:** `propose` now; menu's port deferred until its
trigger fires.

## 017 — "you may be overinvesting in the easiest failure to count"

Codex's standing caution, recorded in its own words because future
prioritization will trip over exactly this.

Reviewing the four-layer parse hardening, codex's central charge was that it
**hardens syntax while under-protecting semantics**: no layer catches a
well-formed reply that is wrong in a way the schema permits — wrong paths,
stale tree assumptions, inverted conditions, fabricated evidence summaries.
*"Your bigger risk is valid-looking wrongness."*

**Accepted as description; it redirects nothing.** Valid-looking wrongness
is not catchable at a parse boundary. It is caught by adversarial structure
and verification-by-execution — the debate, the fresh-context pass,
evidence-precedes-rejection, step-0 spec-vs-tree reconciliation, and a human
reading. That is where every instance has in fact been caught, including
this week's. The next three rungs (`propose`'s diverse sketches, the
criteria red-team, the conductor) are semantic hardening by construction.

**Why it is filed anyway:** counted failures are seductive because they
produce satisfying graphs. A malformed-reply rate is measurable; "the plan
was plausible and wrong" is not. The pull toward the countable is a
prioritization hazard, and this file is where it should be tripped over.

## 018 — evidence-precedes-rejection, NARROWED (amends 014)

014's burden is **scoped to falsifiable claims about the current tree or
runtime behavior**. It does not apply to conceptual disagreement.

**Why narrowing strengthens it:** an unscoped burden teaches the loop to
relabel empirical objections as conceptual ones to dodge the cost, or to
flood the thread with low-value command/output spam to satisfy the form.
Either way: process compliance without epistemic improvement. **A rule that
incentivizes evasion of itself is worse than a narrower rule that holds.**

This is the representable-window lesson applied to a review contract. The
narrowing came from codex — the reviewer role whose colleague's wrong
concessions produced 014 in the first place.

## 019 — the one-retry bound is a WAGER, with its revisit condition attached

Codex: *"`exactly one retry, ever` is not a principle. It is a wager."*
Accepted, with the wager's terms recorded rather than the claim softened:

  * **Kept structural** — straight-line code, no loop, no counter, no knob.
    Revisiting is a deliberate rewrite, never a drift or a config change.
  * **The tribunal is the instrumentation.** cured / recurred / timed_out /
    not_ok per message type, under the conservation invariant.
  * **Revisit condition:** if `recurred` is a large share of issued
    re-prompts, one retry is under-powered and the wager loses. If `cured`
    is near zero, the retry is buying nothing and 0 is correct.

A wager that is cheap to revisit, expensive to change accidentally, and
measured continuously is what a principle looks like before its evidence
arrives.

## 020 — a ruling can be correct for the tree it was written against, and stale an hour later

**Occurred:** 2026-07-20. A ruling on human-pick recording was refuted by
codex on four tree-verified claims and AMENDED — the first ruling in this
campaign to be attacked deliberately and lose.

**What failed was the PREDICATE, not the principle.** The ruling held that
*the record attributes a human pick's win to its actual cause* and that
*concurrence is cheap, deviation is articulate*. Both survived; the
amendment is built on them. What broke was the boundary condition: the
ruling required a stated reason on a **split**, when the harness's authority
line had since grown to **three** guards — `split ∨ degraded ∨
¬provider_independent`. An unsplit-but-same-provider panel would have had
its human pick auto-labeled as cheap ratification, when that is precisely
the correlated-evidence case ruled too weak for autonomous authority ONE
SLICE EARLIER.

**The lesson generalises the spec-vs-tree rule upward:**

> **Rulings go stale exactly like specs do.** A ruling that names a
> condition (`split`) rather than a concept (*any authority guard fired*)
> is pinned to the tree at its moment of writing, and the tree moves.

Step-0 reconciliation was adopted for specs after three wording defects. The
same defect class reached a ruling, from the decider's seat, within a day —
which says the hazard is structural, not a property of who is writing.

**Where it recurred is the finding inside the finding:** this is the
independence gap ONE LAYER UP. Slice 2b stopped the selection record from
collapsing correlated agreement into independent agreement; the ruling would
have re-collapsed the same distinction at the RATIONALE layer. A class
closed at one layer reappears at the next unless the closure is stated as a
concept.

**And the fatal counterargument was flagged in advance.** The decider named
the independence-gap parallel as joint (1)'s strongest objection when
commissioning the refutation — then it proved fatal. Arming a refuter with
the best weapon against your own position is how a ruling gets tested rather
than confirmed.

**Provenance of the amendment (recorded because rulings are artifacts too):**
refuted by codex on four tree-verified claims; predicate corrected from
split-only to the three-guard disjunction; "corroboration, never cause"
promoted from labeling convention to schema with typed fields, after codex
showed no typed home for it existed.
