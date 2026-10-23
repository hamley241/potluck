# Slice: the station exports, imports and delivers — the round boundary becomes data

**Authority: panel ruling, `conductor/docs/decisions/001-fix-round-delivery-boundary.md`** (codex +
kimi, independently, two rounds of three; kimi conceded; codex SHIP on the record at r2). That
document is the design authority for this slice. Where this spec and the ruling disagree, the ruling
wins and the disagreement is a finding.

**This slice is potluck-only.** It adds the station contract the ruling requires. The conductor's
switch to it is the NEXT slice, in the conductor's repo, and nothing here may edit that repo.

## Why

The conductor's fix round re-delivers round 0's work. `_RunWorktree.enter()` cuts the worktree at the
operator checkout's committed HEAD; round 0 delivers uncommitted change and moves no ref; so round 1
recomputes round 0's **complete** patch and `git apply`s it over the copy round 0 already delivered.
For an already-delivered new file the refusal is deterministic. A second defect in the same
mechanism, conceded by both reviewers: even if round 1's delivery *succeeded*, round 1's doer and
gate never see round 0's work — the run gates a tree that is **not** the candidate.

The ruling moves the delivery boundary to **one delivery per accepted task**. The station stops
deciding when work reaches the operator's checkout; it exports an attested result, can import one,
and delivers only when asked. **No worktree outlives its run** — what crosses the round boundary is
bytes plus a manifest.

## What this slice builds

Three capabilities. The names below are this spec's; argue for better ones in review if they are
wrong, but the semantics are the ruling's.

**1. Export (build without delivering).** A `potluck fix` run that is told to export does not apply
anything to the operator's working tree. On **PASSED** it writes a portable result into a
caller-supplied, out-of-tree directory:

* a **versioned manifest** naming: this run and round, the repository identity, the **original task
  base commit**, the imported-result digest (or its explicit absence), the cumulative patch digest,
  the output tree identity and digest, and the station artifact digest;
* a **self-contained binary patch from the ORIGINAL task base to the complete output** — additions,
  deletions, exec and symlink modes, binary bytes, and untracked-non-ignored files, matching the
  boundary `snapshot_tree()` already draws;
* the run's `station-run.json` and the **exact** spec, acceptance and gate inputs needed to resolve
  its bindings.

Payload filenames are **manifest-relative**; every payload carries its digest in the manifest. The
**manifest is published last** — an export whose payloads are incomplete must not be acceptable to
any later operation. A PASSED run that cannot complete its export **fails**: the export is the
round's product, and a silently missing export becomes a confusing refusal one round later.

**2. Import (seed a round from a previous round's attested result).** Given a previously exported
result, a run creates its worktree as it does today — fresh, detached, at the operator's committed
HEAD — and then:

* **verifies the patch against the digest declared in the manifest BEFORE applying it;**
* applies it;
* **recomputes the resulting tree's digest and refuses, fail-closed, unless it equals the imported
  result's attested `output_tree_digest`.**

Only then does the doer start. The point is not convenience: it makes round r+1's input
*definitionally* round r's gate-passed, review-targeted output, and it catches a corrupted or
hand-edited export before any model call is spent on it.

**Restore and resume must understand the imported input.** A retry or a `--resume` of an importing
run restores the **digest-verified input snapshot**, not an empty base — and **import must not be
implemented by disabling restoration.** A resume that silently discards the previous round's work is
the defect this slice exists to remove, reintroduced one layer down.

**3. Deliver (apply an accepted result, no model calls).** A separate operation that takes a result
**selected by its manifest digest** and applies its cumulative patch to the operator's checkout once.
It runs no doer, no gate and no review. Its safeguards are **requirements, not implications of the
tests below**:

* it requires the **expected repository identity and base**, *and* that the operator checkout still
  matches the **captured clean baseline** the manifest names;
* it **preserves the real index and refs** — no ref is written, the real index is not used as scratch;
* it applies **all-or-nothing**;
* on conflict it **retains the result** and returns a **structured refusal** — never an automatic
  rebase, never an overwrite.

The baseline check is the load-bearing one. Without it an implementer accepts an unrelated operator
edit *because the patch still applies* — the applicability-versus-equality gap that decided the
ruling: a clean apply over a foreign edit leaves the station attesting one tree while the reviewer
reads another.

## What must not change

* **`head != base` stays a hard refusal**, for export eligibility and for delivery. Importing a
  result changes files, not HEAD; the doer still has no reason to commit, and if it does, the
  review-diff gap is unchanged.
* **`station-run.json` stays v1**, and the **original base stays `base_commit`** — the landing
  verifier binds it to the landed commit's parent, and `output_tree_digest` must keep meaning the
  digest of the run's full final tree. New transport metadata belongs in the **export manifest**, a
  companion artifact. *Silently reinterpreting v1 is unacceptable:* if this slice cannot be built
  without changing v1's meaning, **stop and say so** — the verifier/schema slice comes first, in
  `harness_core`, before the conductor consumes it.
* **`--allow-dirty`'s semantics and help text are untouched.** It skips the clean-tree precondition,
  drops the restore and logs `tree_hygiene_disabled`; it does not seed anything. Its honest help is
  the one piece of this mechanism that was never misdescribed.
* **Ordinary CLI delivery behaviour is preserved for existing callers.** A `potluck fix` invoked as
  it is today — no export, no import — still delivers into the operator's tree on PASSED and still
  writes nothing on any other outcome.
* **No worktree is intentionally retained.** The transport is a patch file; it has none of the
  orphan and TOCTOU properties that caused the cleanup sweep to be written and then removed.
* **One repository.** potluck only. The restore is single-repo, so an edit outside this checkout
  escapes it with no restore and no record.

## Already-known traps

* The **delivery-failure message must stop asserting a cause it cannot know.** Today it says the tree
  *"changed during the run"*; in a fix round it changed **between** runs, by the previous round's own
  delivery. The refusal must state captured **expected-vs-observed** facts, distinguish **baseline
  drift** from **patch conflict**, and say where the retained result is — in the **operator-facing
  message**, not only in a log. An inaccurate refusal message is how the earlier defects reproduced.
* The unapplied-patch `mkstemp` fallback **dies** with this slice: the retained export *is* the copy.
  A temp-file name in a log is not retained evidence.
* `work_tree_oid` in a `TreeSnapshot` is a **loose object left unreachable** once the worktree is
  removed; it survives only until a `git gc`. A result is addressed by its **manifest digest** and
  carries **bytes** — never by a bare tree OID.

## Review budget

**3 rounds, declared.** If it is exceeded, the overrun is declared in the receipt. The cap terminates
findings-chasing, not verification.
