# Acceptance: the station exports, imports and delivers

Graded against `export-import-deliver-spec.md` and the panel ruling it cites. Every criterion below
was checked for **reachability on this checkout** before being written — the recurring failure in
this fleet is an absolute nobody tested, and an unmet absolute left standing makes the record say the
work failed its own acceptance. **If a criterion turns out to be unreachable or wrong, amend it in
writing here and say why; do not leave it standing and do not quietly drop it.**

## The gate

**A1.** `./test` exits 0 with `ALL SUITES PASS`, with **at least** the 648 checks `main` has today.
The number must not be cited as an exact value: this slice adds tests, so it rises.

**A2.** Every new behaviour is covered by a test in the repo's own suite, and each **negative** case
below is a constructed failure fixture that is required to fail **and to name the reason** — not a
test that merely asserts a boolean.

## Ordinary callers are untouched

**A3.** A `potluck fix` invoked as today — no export, no import — still applies its change to the
operator's working tree on PASSED, still writes nothing on any other outcome, and still removes its
worktree on every path. Proven by a test over a temporary repository, not by inspection.

**A4.** `--allow-dirty`'s own help text and its three effects (skip the clean-tree precondition, drop
the restore, log `tree_hygiene_disabled`) are **unchanged**. A source-level check pins the help
string so a future edit reddens.

## Export

**A5.** A PASSED run told to export writes, into the caller-supplied out-of-tree directory: a
versioned manifest, the cumulative patch, the run's `station-run.json`, and the exact spec,
acceptance and gate inputs. Payload names are manifest-relative and every payload's digest is in the
manifest.

**A6.** The **manifest is published last.** A directory holding payloads but no manifest is **not
acceptable** to import or to deliver, and the refusal says so. (Constructed fixture: remove the
manifest.)

**A7.** The cumulative patch is **from the original task base to the complete output**, and carries
additions, deletions, an exec-bit change, a symlink, binary bytes and an untracked-non-ignored file.
Ignored paths stay out, matching the boundary `snapshot_tree()` already draws.

**A8.** A PASSED run whose export cannot be completed **fails** rather than reporting success.

**A9.** A **non-PASSED** run writes nothing to the operator's tree **and** leaves no acceptable
export. (Constructed: a gate-failing run.)

## Import

**A10.** Import **verifies the patch against the manifest's declared digest before applying it**, and
refuses fail-closed on mismatch, naming the file and both digests. (Constructed: flip one byte.)

**A11.** After applying, the resulting tree's recomputed digest is compared with the imported result's
attested `output_tree_digest`, and a mismatch refuses fail-closed **before any doer call is
dispatched**. The test asserts the doer was not called.

**A12.** A `--resume` of an importing run reconstructs the **digest-verified input snapshot**, not an
empty base. Import is **not** implemented by disabling restoration: a source-level check shows the
restore path remains wired for importing runs.

## Deliver

**A13. The two-round case, end to end.** Round 0 exports a result that adds a new file; round 1
imports it, modifies that file, and exports a replacement cumulative result; delivering **only round
1's** result applies once and leaves the operator's tree holding round 1's content. The patch that
defeats today's mechanism — the already-delivered new file — does not recur.

**A14. Baseline drift refuses even when the patch would apply.** With the operator checkout carrying
an unrelated foreign edit, delivery **refuses** because the captured clean baseline no longer
matches, *although applying the patch would have succeeded*. This is the applicability-versus-equality
gap and it is the single most important negative test in this slice.

**A15.** Delivery **retains** the result on refusal, writes nothing to the operator's tree, performs
no rebase and no overwrite, and **writes no ref**; the real index is unchanged. Asserted by comparing
`HEAD`, every ref, and the index's tree projection before and after.

**A16.** Delivery selects a result **by manifest digest**. A selection whose digest does not match the
named directory's manifest refuses. A pathname alone never authorizes delivery.

**A17.** Delivery runs **no doer, no gate and no review** — asserted by a test that would fail if any
model call were dispatched.

**A18.** `head != base` remains a hard refusal for export eligibility and for delivery, with its
existing reason text intact.

## The message, and the dead fallback

**A19.** A delivery refusal states captured **expected-vs-observed** facts, **distinguishes baseline
drift from patch conflict**, and names where the retained result is — in the **operator-facing
message**. It no longer claims the tree *"changed during the run"* when it did not.

**A20.** The `mkstemp` unapplied-patch fallback is **gone from the delivery path**: the retained
export is the copy. A test asserts the refusal names a durable path, not a temp file.

## Boundaries

**A21. `station-run.json` stays v1.** The artifact's field set and their descriptions are unchanged,
`base_commit` still names the run's starting HEAD, `output_tree_digest` still digests the run's full
final tree, and no new required field appears. All transport metadata lives in the export manifest.
**If the slice cannot be built without changing v1, it stops and says so** — that is an acceptable
outcome of this slice and a better one than a silent reinterpretation.

**A22. No worktree is intentionally retained**, and after every path — pass, fail, refusal, resume —
no `.<repo>.potluck-run-*` directory and no `.git/worktrees` entry for it remains. Pre-existing
hard-kill orphan gaps are declared, not closed here.

**A23. One repository.** No file outside this checkout is modified. I verify this after the run by
checking `git status` in every sibling repo, because potluck's restore is single-repo and a foreign
edit would escape it silently.

## Out of scope — do not do these here

The conductor's switch to exported results, its review-materialization path, its record schema bump
and its four false `on the dirty tree` docstrings are the **next** slice, in the conductor's repo.
Reviewing a reconstruction is conductor-side machinery by the ruling. Touching any of it here makes
the slice cross-repo and un-restorable.
