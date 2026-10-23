• I'll ground this in the actual code and ruling before giving opinions. Let me read the ruling, the canonicaliser, and the potluck export/import implementation.

• Now let me look at the current `deliver()` and the CLI wiring for `--import-dir`/`--export-dir`.

• Let me verify the digest machinery and the clean-tree precondition — these matter for whether Q2(a) is implementable as claimed.

• One more check — the digest domain of `output_tree_digest` (worktree vs tree object), since Q2 hinges on it.

• I've read the ruling, the landed export/import code, the conductor canonicaliser, and the digest machinery. Opinions below, each grounded in the code that's actually there.

  ## Q1 — (a), with one correction to how you sold it

  Delivery reads the operator checkout's *current* `git remote get-url --push origin` (fail-closed if unreadable) and its root commits, and requires both to equal the manifest's recorded `push_url_raw` and `root_commits`, refusing on either and naming both sides. The refusal and spec state plainly: this is evidence-continuity against the export's record; canonical destination comparison is the caller's job, done with the fleet's one canonicaliser in `push_safety`. That division is already the landed architecture — part 1's export comment says it verbatim: "The consumer canonicalises with that one implementation; the station reports what it observed" (`harness/cli.py:902-910`). Option (a) is just the delivery-side completion of a decision part 1 already committed and got receipted.

  **Where your framing is wrong:** you sold root commits as the intrinsic, unspoofable anchor and the URL as recorded evidence. It is the reverse that carries the security weight. Root commits do **not** distinguish a repository from its fork — a fork is a server-side clone and shares the root commits. Your own part-1 comment says exactly that ("a basename plus root commits cannot distinguish a repository from a FORK checked out under the same directory name", `harness/cli.py:902-904`). The load-bearing check under (a) is the raw URL string — a fork has a different URL; root commits bind against delete-and-recreate / URL reuse / whole-history replacement. Keep both checks, but know which one does the work.

  **(b) is dead, and sharper than "moves the gap":** the comparison you propose — caller's canonical identity vs potluck's observation — is one potluck *provably cannot perform* without a canonicaliser. So (b) collapses into either "the caller does the whole comparison" (the safeguard leaves potluck, making the ruling's REQUIRED-safeguard language untestable in the potluck slice) or (c). There is no middle setting.

  **(c) belongs in its own harness_core prerequisite slice, and only if (a) shows real pain.** It changes the fleet's shared core to solve a problem the fleet already solved at another layer, and the ruling explicitly says "No menu or harness_core change is necessary". The failure mode of (a) is a *false refusal* — spelling variants (`b.git` suffix, `user@`) that `canonical_repo` would map together — which is recoverable at an operator-run step. If that becomes operational noise, do (c) later with both repos vendoring one implementation, on the reviewgate pattern you already have. Not preemptively, not inside this slice.

  **One hole (a) must close that you didn't mention:** export allows `push_url_raw: None` when origin is unreadable (`harness/cli.py:911-913`). String equality is then vacuous and identity rests on roots + basename — which part 1 already deemed insufficient. My call: delivery refuses a manifest with no recorded push URL, naming why, fail-closed. Fleet repos always have origin (`push_safety` requires it); the bitten cases are local edge cases that can add a remote and re-export. And do **not** compare `repository.name` — checkouts get renamed; the basename is display, not identity, and refusing on it manufactures false refusals.

  ## Q2 — (a). "Captured" does not mean "recorded in a new field at export time"

  Write the acceptance criterion as: delivery refuses unless **(i)** the operator checkout's HEAD equals `manifest.base_commit` **and (ii)** the digest of the operator tree — computed with the `add -A` recipe, the same boundary as `output_tree_digest` — equals the digest of `base_commit^{tree}`. Both halves run on landed machinery today: `station_run._output_tree_digest` over the operator path (`harness/station_run.py:86-105`) and `checkpoint._tree_digest` over the resolved tree (`harness/checkpoint.py:216-221`); the temp-index recipe exists precisely so the real index is untouched.

  Why this is faithful, not weaker: under part 1's clean-tree export precondition, `base_commit` *fully determines* the operator tree at export time. The export captured the baseline; the capture **is** the manifest. The ruling's rationale — closing the applicability-without-equality gap — is served by equality with `base^{tree}`, because that is exactly the tree the exported patch transforms. Nothing is missing; there is nothing to record.

  **(b) has two defects.** In the clean case the field is derivable — a second source of truth that can only disagree with `base_commit` and raise "which wins". In the `--allow-dirty` case it records the operator's *dirt* as "the clean baseline", and delivery would then bless a target whose delivered tree cannot equal the attested output — it converts the ruling's REQUIRED check into its own bypass. Note (a) handles `--allow-dirty` exports gracefully for free: such a result simply isn't deliverable until the operator tree actually is `base^{tree}` — the conservative, correct outcome, since the run recorded no clean baseline to match.

  **(c)'s audit field is harmless but I'd argue against it:** everything it attests is derivable from `base_commit`, and the criterion must compare against `base^{tree}` anyway, so the field is decoration future readers will think they must trust. The ruling's required record — "delivery's expected and observed target state" — belongs in the conductor's committed log, where *observed-at-delivery* state lives; no export-time field can hold that.

  One precision to pin in the criterion so nobody "simplifies" it later: "clean" must mean the digest boundary (tracked + untracked-non-ignored, tracked-then-ignored retained, index seeded from HEAD), not `git status` porcelain; and the HEAD half is not optional — HEAD-equality alone is exactly the empty-commit hole part 2 closed on the import side (`test_moved_head_refused`).

  ## Q3 — (b). Refuse the combination, at argument validation, before anything runs

  The only fleet consumer of `--import-dir` is the fix-round mechanism, and under the ruling every round exports its cumulative result. Import-without-export is precisely the conductor misconfiguration that should fail loud, fast, and before a doer call is spent — the same fail-before-work discipline part 2 applied to overlapping directories. The human cost is real but small: round 1 runs with `--import-dir --export-dir`, then the explicit `deliver` by digest — two commands, and the second carries the safeguards and the repaired refusal message. Nothing the ruling promised is lost: "ordinary CLI delivery behaviour preserved for existing callers" means plain runs with **no import flag**; the import flag has no pre-existing callers outside the flow the ruling just redesigned, so (b) removes no ruled mode — and no live caller exists anywhere yet, since the conductor switch is ruling slice 2.

  **Framing corrections:** you called (a) "a SECOND delivery path" — it's a third. Ordinary direct `deliver()` survives forever by ruling mandate, and (a) would add the one path that lands an imported cumulative candidate with none of the REQUIRED safeguards (no baseline check, no identity check, grandfathered semantics) — exactly the shape the panel spent 200 lines eliminating. And "bypass" cuts both ways: keeping (a) means part 3 must forever specify, test and maintain "is `deliver()` of an importing run supposed to baseline-check or not?" — the ambiguity you said you want gone.

  **(c) is invented work:** it drags export's eligibility surface (run-dir, export-task, gate bytes, artifact) onto runs that never asked to export, needs a phantom task identity, creates an orphan temp export with undefined retention/cleanup semantics — and `deliver()` still exists for plain runs, so it buys "one implementation for imports" at the price of a hidden second *export* path with more contract surface than the mode it replaces.

  The test you asked for becomes: importing run with `--export-dir` PASSED → export; explicit deliver by that manifest digest → complete candidate lands once; and separately `--import-dir` without `--export-dir` → refusal naming both flags and the recovery. Part 2's tests asserting direct delivery of an importing run get amended in part 3 with a written spec amendment — a contract evolving across its declared slices, not a regression.

  ## One thing you didn't ask about

  "Preserve the real index and refs" is the cheapest of the four safeguards: the landed machinery already models it — `change()` and `_output_tree_digest` stage into temp indices and leave the real index alone. Delivery should apply with plain `git apply` (working tree only, all-or-nothing), verify the post-state with the same recipe, never commit. Worth one acceptance criterion: on a structured refusal, operator HEAD, refs, index and tree are byte-identical to pre-invocation — nothing to roll back beyond what `git apply` itself guarantees.

  **Bottom line: Q1 (a) with a refuse-on-null-URL rule, Q2 (a) with the two-part criterion, Q3 (b) refusing at validation time.** The through-line: the ruling's safeguards all attach to the manifest-digest boundary; every answer above either strengthens that boundary or refuses to create a second one.

