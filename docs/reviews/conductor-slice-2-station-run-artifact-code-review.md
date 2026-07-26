# conductor build slice 2 — station-run artifact (§4f producer) — codex code-review findings (verbatim)

Independent, cross-provider CODE review of **conductor build slice 2** (potluck's
station-emitted run artifact, the §4f producer half; producer-first per the decider
ordering ruling 2026-07-25), by **codex (OpenAI), model `gpt-5.6-sol`** — read-only
sandbox (`codex exec -s read-only`), doer Claude (Anthropic). Range
`6fa6113..ab45dc9` (potluck only). Three rounds; verdicts reproduced
VERBATIM below.

The convergence: r1 (2 BLOCKING + 3 MAJOR) — terminal-state recomputability, the
categorical emission contract, gate-timing/lossy-hash/version-pin; r2 (1 BLOCKING + 1
MAJOR + 1 MINOR) — the empty-index seed mis-modelling `git add -A` for
tracked-then-ignored files, and stale-artifact safety; r3 CLEAN. The load-bearing
`output_tree_digest` now equals a real `git add -A && commit` tree (proven against a
real-commit oracle including the tracked-then-ignored case), so the later reviewgate
verifier can recompute it against `reviewed_head^{tree}`.

---

## Round 1 — verbatim

> ## BLOCKING
>
> - [harness/station_run.py:97](/Users/gp/workspace/masala/potluck/harness/station_run.py:97) — `terminal_state` is not independently recomputable from the declared inputs and repository. Two runs can have identical spec, acceptance, gate, base, and output tree yet terminate differently—for example, `passed` versus `escalated_disagreement` due solely to reviewer output. A future verifier must trust the artifact’s claim unless the artifact commits sufficient execution evidence from which the terminal state can be derived. Either add canonical terminal evidence/transcript commitments or narrow the contract so this field is explicitly cross-bound/validated rather than “recomputed.”
>
> - [harness/cli.py:397](/Users/gp/workspace/masala/potluck/harness/cli.py:397) — ordinary `potluck fix` runs emit no artifact unless `--run-dir` or `--resume` is supplied. The categorical §4f producer contract says `potluck fix` emits the artifact; `potluck fix --spec ...` completes with `_art_dir is None` and produces nothing. Require an artifact output directory for conductor-driven runs or establish a default sidecar destination so every in-scope run emits one.
>
> ## MAJOR
>
> - [harness/station_run.py:89](/Users/gp/workspace/masala/potluck/harness/station_run.py:89) — `gate_digest` is computed from the gate’s terminal contents, not the gate input captured when the run began. The doer can modify `.claude/verify.sh` during the run, after which the artifact attests the modified file rather than the handoff bundle’s gate input. Capture the gate bytes/digest before dispatch and carry that immutable value through fresh and resume execution.
>
> - [harness/station_run.py:95](/Users/gp/workspace/masala/potluck/harness/station_run.py:95) — the purported exact-byte gate digest is lossy. Invalid UTF-8 sequences are replaced, so distinct byte files can collapse to the same decoded text and digest. Hash `gate_bytes` directly with SHA-256; do not decode them.
>
> - [harness/station_run.py:50](/Users/gp/workspace/masala/potluck/harness/station_run.py:50) — schema identifiers are unconstrained integer/string defaults. `load_station_run_artifact()` accepts `schema_version: 999` or an arbitrary `canon_version`, causing this v1 reader to interpret unknown future formats as v1. Use `Literal[1]` and `Literal["potluck-station-run-v1"]`, or dispatch on the raw version before v1 validation and fail closed on unknown versions.
>
> ## MINOR
>
> None.
>
> ## NIT
>
> None.
>
> The temp-index tree construction itself matches `git add -A && commit`: Git preserves symlink/executable modes, D/F transitions, leading-dash paths, empty trees, and committed submodule gitlinks; ignored and dirty submodule-internal content is excluded consistently. Because run directories are already refused inside the worktree, writing `station-run.json` after calculating the digest does not alter the product tree. Best-effort emission is acceptable only once the later required-field verifier fails closed on a missing artifact.
>
> Test execution was blocked by the read-only environment’s inability to create a temporary directory; static inspection confirmed the test’s intended coverage but also the omissions above.
>
> **VERDICT: CHANGES REQUIRED**

---

## Round 2 — verbatim

> BLOCKING
>
> - [harness/station_run.py:95](/Users/gp/workspace/masala/potluck/harness/station_run.py:95) — `_output_tree_digest()` initializes the temporary index with `read-tree --empty`. That does not model the conductor’s `git add -A && commit`, which starts with the repository’s HEAD-backed index. Concrete failure: if a committed file later matches `.gitignore`, the real `git add -A` retains and updates that tracked file, while the empty-index version treats it as ignored and omits it. The artifact digest then differs from `reviewed_head^{tree}`, so a genuine station run fails later verification. Initialize the temporary index from the appropriate base/HEAD tree before `git add -A`, and add a tracked-then-ignored regression test.
>
> MAJOR
>
> - [harness/cli.py:413](/Users/gp/workspace/masala/potluck/harness/cli.py:413) — a reused run directory can retain a previous `station-run.json` when the new emission fails before writing. Because emission only warns and `terminal_state` is intentionally attested rather than recomputed, an old `passed` artifact can be mistaken for the latest run when inputs/base/output happen to match but the latest run escalated or aborted. Remove or invalidate any existing artifact before starting a station run, then publish the new artifact atomically; downstream consumption should also bind it to the current run identity.
>
> MINOR
>
> - [harness/test_station_run.py:105](/Users/gp/workspace/masala/potluck/harness/test_station_run.py:105) — the “manual” oracle reproduces the same empty-index algorithm as production, so it cannot expose the tracked-ignored divergence. The equality test should compare against an index initialized from HEAD or, preferably, an actual `git add -A && git commit` tree.
>
> NIT
>
> - None.
>
> The round-one folds otherwise hold: gate bytes are captured before the fresh run, exact-byte hashing is used, the recomputable/attested distinction is explicit, resume retains the checkpoint base, and both version discriminators are fail-closed.
>
> VERDICT: CHANGES REQUIRED

---

## Round 3 — verbatim

> BLOCKING
>
> None.
>
> MAJOR
>
> None.
>
> MINOR
>
> None.
>
> NIT
>
> None.
>
> The round-2 corrections hold:
>
> - `_output_tree_digest()` seeds a fresh index from `HEAD`, then applies `git add -A`; its written tree therefore matches the conductor’s eventual commit across tracked-ignored files, deletions, symlinks, executable bits, D/F changes, and gitlinks.
> - The real index remains untouched.
> - Fresh runs bind `base_commit` to the starting `HEAD`; resumed runs correctly retain the checkpoint’s original `repo_base_commit`.
> - The gate digest covers exact start-gate bytes.
> - Stale artifacts are removed before execution, and replacement is atomic.
> - Missing emission cannot masquerade as success: it leaves no artifact, and the later required verifier slice will fail closed.
> - `Literal` version pins plus `extra="forbid"` provide a clean fail-closed v1 boundary for future schemas.
> - `terminal_state` remains attested rather than independently recomputable, consistent with the explicitly ratified §4(f) limitation.
>
> Focused test execution was prevented by the review environment’s read-only filesystem: Python could not create a temporary directory. Static inspection and `git diff --check` found no defect; the supplied live-test evidence covers the inaccessible runtime check.
>
> VERDICT: RATIFIED

---

**Ratified at round 3, codex CLEAN (0 findings), VERDICT: RATIFIED.** Recorded as the reviewgate SHIP-class landing authorization (SHIP is the gate's only token; RATIFIED is codex's verbatim clean word). `terminal_state` is station-attested per the ratified §4(f) declared limitation (threat-model B).
