# Slice: the station DELIVERS a selected result — part 3 of 3

**Authority: panel ruling, `conductor/docs/decisions/001-fix-round-delivery-boundary.md`.** Where this
spec and the ruling disagree, the ruling wins and the disagreement is a finding.

**Parts 1 and 2 have landed** — export as PR #19 (`6278131`), import as PR #20 (`fd95c82`), gate 793,
receipts `docs/reviews/export-slice-receipt.md` and `import-slice-receipt.md`. `harness/export.py`
gives you `load_export()` (fail-closed on the manifest's shape, every payload digest, and agreement
with the shipped `station-run.json`); `harness/cli.py` gives you `_RunWorktree.change()`,
`apply_import()`, `_overlaps()`, `_refuse_links_into_import()` and `_write_nofollow()`. **Use them.**

**This slice completes the contract.** After it, the conductor can stop delivering per round — that
switch is the NEXT slice, in the conductor's repo, and nothing here may edit that repo.

## What this slice builds

**An explicit delivery operation.** It takes a result **selected by its manifest digest**, applies
that result's cumulative patch to the operator's checkout **once**, and runs **no doer, no gate and no
review** — a test must fail if any model call is dispatched.

**Its safeguards are REQUIRED, not implied by the tests.** The ruling lists them, and the review that
produced it made the first one decisive:

* the **expected repository identity** and the **expected base**;
* the operator checkout still matching the **captured clean baseline** — *this is the load-bearing
  one*: without it an implementer accepts an unrelated operator edit **because the patch still
  applies**, and then the station has attested one tree while the reviewer read another. **Patch
  applicability is not whole-tree equality.** That single argument is why candidate 1 won the panel;
  a delivery that omits this check throws away the ruling;
* **preservation of the real index and every ref** — no ref is written, the real index is not used as
  scratch;
* **all-or-nothing** application;
* on conflict, the result is **RETAINED** behind a **structured refusal** — never an automatic rebase,
  never an overwrite.

**The failure message is repaired here.** Today `deliver()` says the tree *"changed during the run"*,
which in a fix round is false — it changed **between** runs, by the previous round's own delivery. The
refusal must state captured **expected-vs-observed** facts, **distinguish baseline drift from patch
conflict**, and name **where the retained result is** — in the **operator-facing message**, not only
in a log. An inaccurate refusal message is how the earlier defects reproduced, and it is the one
remaining piece of the ruling's "what the conductor must record" that lives on potluck's side.

**The `mkstemp` fallback dies.** `deliver()`'s unapplied-patch copy currently goes to an anonymous
`$TMPDIR` file whose path is named only in the station output. The retained export **is** the copy.
The ruling's words: *a committed log containing a temp-file name is not retained evidence.*

## What must not change

* **Ordinary callers are untouched.** A `potluck fix` with no export, no import and no delivery flag
  behaves exactly as today.
* **`station-run.json` stays v1**, `base_commit` still the run's starting HEAD, `output_tree_digest`
  still the digest of the full final tree. **If this slice cannot be built without changing v1, it
  stops and says so.**
* **No ref is ever written** by delivery, and no worktree is intentionally retained.
* **`--allow-dirty`'s semantics and help text are untouched.**
* **One repository.** potluck only.

## The three design points, ruled before the build

**codex and kimi were asked independently, neither shown the other's answer. Both chose (a), (a),
(b).** Verbatim questions and answers: `deliver-by-digest-opinions/`. The reasoning lives there and in
this slice's receipt; what follows is only what you must build.

### 1. Identity — compare the recorded evidence exactly

Refuse unless the operator checkout's current `git remote get-url --push origin` **and** its root
commits equal the manifest's `push_url_raw` and `root_commits`, naming both sides. Canonical
destination comparison stays the caller's job, with the fleet's one canonicaliser in the conductor's
`push_safety` — part 1 committed that division and had it receipted.

* **The raw URL carries the weight**, not the root commits: a fork is a server-side clone and SHARES
  the root commits. Roots bind against delete-and-recreate, URL reuse and history replacement.
* **Refuse a manifest whose `push_url_raw` is `null`** (part 1 allows it when origin is unreadable):
  equality against `null` is vacuous and identity would fall back to roots plus basename, which part 1
  already deemed insufficient.
* **Never compare `repository.name`** — checkouts get renamed; the basename is display, not identity,
  and comparing it manufactures false refusals.
* State the accepted limitation in the contract: this is conservative evidence-continuity, so
  equivalent URL spellings (`.git`, `user@`) can cause a FALSE refusal, recoverable by the operator.

### 2. The captured clean baseline — three parts, no new manifest field

> The delivery baseline is the **clean checkout of the manifest's `base_commit`**. Delivery requires
> operator HEAD to equal that commit, **the real index to represent that commit's tree**, and the
> operator working tree — untracked-non-ignored included — to match that tree.

* **The real index is checked independently**, without refreshing or rewriting it. A digest computed
  through a temp index can match the base while the REAL index holds staged changes the working tree
  reverses — reproduced: stage a modification, restore the file's content, and the `add -A` digest
  equals `base_commit^{tree}`'s while `git diff --cached HEAD` still reports `M`.
* **"Clean" means the digest boundary** (tracked + untracked-non-ignored, tracked-then-ignored
  retained, index seeded from HEAD), not `git status` porcelain. Use
  `station_run._output_tree_digest` over the operator path and `checkpoint._tree_digest` over
  `base_commit^{tree}`; both already end in the same `ls-tree -r` fingerprint.
* **HEAD-equality is not optional**: alone it was the empty-commit hole part 2 closed on the import
  side, and the tree half alone cannot see a moved HEAD.
* An export made with `--allow-dirty` is **not deliverable until the operator tree actually is
  `base^{tree}`** — conservative and correct, because that run recorded no clean baseline. Do **not**
  add an `operator_baseline_digest` field: under `--allow-dirty` it would record the operator's dirt
  as "the clean baseline" and turn this required check into its own bypass.
* **Refuse an ignored-path obstruction** that application would overwrite. Ignored files being outside
  the digest boundary is not authorization to clobber them.

### 3. `--import-dir` without `--export-dir` is refused at argument validation

Not a defect today — my earlier claim that it was the redelivery defect was wrong and is retracted —
but it is a THIRD delivery path (ordinary `deliver()` survives by ruling mandate) and the only one
that lands an imported cumulative candidate with **none** of the required safeguards. Refuse early,
naming both flags and the recovery: run the round with `--import-dir --export-dir`, then deliver the
accepted manifest digest. **Ordinary CLI delivery is untouched** — the ruling protects plain runs with
*no import flag*.

**This amends part 2 in writing, deliberately.** Part 2's `test_retry_restores_imported_state`
asserts an importing run delivers directly; that assertion is amended here with its reason recorded
in the test — a contract evolving across its declared slices, not a regression.

### What "once" promises, and what it does not

Delivery enforces an explicit, **digest-bound** application and refuses on baseline mismatch. It does
**not** promise durable exactly-once acceptance across retries, crashes, empty patches, or two
different results for one task — that needs caller orchestration or a delivery-receipt contract. Say
so in the contract rather than implying it because there is one delivery command.

## Review budget

**3 rounds, declared.** If exceeded, the overrun is declared in the receipt.
