# conductor §5 — potluck `fix --gate-file` seam — codex code-review findings (verbatim)

Independent, cross-provider CODE review of **conductor Step-0 §5** (potluck's external
gate-input seam, the §5 hop-2 mechanism), by **codex (OpenAI), model `gpt-5.6-sol`** —
read-only sandbox (`codex exec -s read-only`), doer Claude (Anthropic). Range
`675a3c6..7d085d0` (potluck only). Three rounds; verdicts reproduced VERBATIM below.

The convergence:
- **r1 (1 BLOCKING + 3 MAJOR)** — the external gate had to be made IMMUTABLE and its MODE
  bound: (BLOCKING) resume bound only the digest, not the mode, so an external run could
  resume without `--gate-file`, match an identical-bytes in-tree file, and run the mutable
  in-tree gate; (MAJOR) nothing enforced the gate was outside the tree; (MAJOR) the digest
  wasn't revalidated at execution (capture ≠ executed bytes); (MAJOR) the resume test was
  a false positive.
- **r2 (1 BLOCKING + 1 MAJOR)** — the first fold bound the gate via `run_inputs.json`, an
  editable sidecar NOT part of the checkpoint identity, so editing it downgraded the gate;
  and a null digest made any frozen gate acceptable. Ruling: bind the gate into the
  tamper-evident identity with an explicit version discriminator, not a missing-field
  fallback; the test still needed a real checkpoint.
- **r3 — RATIFIED** (0 findings).

Final design: `--gate-file` requires `--run-dir` + lives outside the tree; the gate bytes
are FROZEN into the run-dir and the immutable copy is run + digested; the digest is the
`CheckpointIdentity.gate_binding_digest` (schema v2, the version discriminator);
`run_inputs.json` carries no gate field; resume resolves the gate from the frozen copy's
PRESENCE (a file fact) and `verify_identity` refuses any downgrade or tamper.

---

## Round 1 — verbatim

> BLOCKING
> - resume binds only the gate digest, not whether the run committed to an external gate.
>   Start with `--gate-file /handoff/verify.sh`, then place identical bytes in
>   `.claude/verify.sh` and resume without `--gate-file`. The digest comparison passes,
>   `build_orchestrator(..., gate_path=None)` selects the mutable in-tree gate, and the
>   doer may rewrite it before execution... Persist and verify the external-gate mode on
>   resume; an external-gate run must require `--gate-file`.
>
> MAJOR — nothing enforces that `--gate-file` is outside the working tree. A tracked path
> such as `--gate-file scripts/verify.sh` passes `_require_gate` and the clean-tree check.
> The doer can then modify that file before the gate runs... Resolve both the gate and Git
> top-level and refuse paths located within the working tree.
>
> MAJOR — the captured digest is not revalidated immediately before execution. If an
> external gate changes after capture—during doer work or an ordinary handoff
> regeneration—the altered bytes are executed, while `run_inputs.json` and the station
> artifact attest the earlier digest... Execute immutable captured bytes, or verify the
> digest immediately before every gate invocation.
>
> MAJOR (test) — the resume test is a false positive. It creates `run_inputs.json` but no
> checkpoint, and lines 184–190 accept any `SystemExit(1)`... Assert the mismatch
> diagnostic and prove checkpoint/doer seams were not reached, or construct a valid real
> checkpoint whose resume would otherwise proceed.

---

## Round 2 — verbatim

> Not ratified.
>
> - BLOCKING — resume mode is still downgradeable through mutable `run_inputs.json`.
>   `cli.py` uses `.get()` for both new fields, and `checkpoint.py` does not include either
>   field in the checkpoint identity. For an external run, edit `run_inputs.json` to remove
>   `external_gate` and `gate_digest`—or set `external_gate: false` and remove the digest—
>   then provide any in-tree `.claude/verify.sh`. `_resolve_resume_gate` returns the in-tree
>   mode, and checkpoint verification cannot detect the alteration. Setting
>   `external_gate: true` while deleting only `gate_digest` also makes any existing frozen
>   gate acceptable because the comparison is conditional on the digest being non-null.
>   New-format runs must require correctly typed fields and bind them into
>   immutable/checkpointed identity; compatibility with fieldless old runs needs an explicit
>   version discriminator, not missing-field fallback.
>
> - MAJOR (test) — the positive resume test still does not prove checkpoint loading was
>   reached... Assert the specific checkpoint-missing diagnostic, or install a real
>   checkpoint and verify a later controlled seam. Add downgrade tests that remove each new
>   field and set `external_gate` false with matching in-tree bytes.
>
> [Other mechanics confirmed correct: fresh external requires --run-dir, rejects in-tree,
> freezes outside the tree, executes the frozen path; os.replace atomic; digest from the
> frozen bytes; resolve-before-chdir; git clean -fdx cannot reach a valid run dir;
> post-freeze source mutation no longer affects executed bytes.]

---

## Round 3 — verbatim

> RATIFIED.
>
> The round-2 BLOCKING and test MAJOR are closed.
> - `run_inputs.json` contains no gate-related fields; editing it cannot select or
>   downgrade gate mode.
> - Resume mode derives solely from `frozen-gate.sh` presence. Deletion yields `"in-tree"`
>   and modification yields a new SHA-256; both mismatch an external checkpoint's committed
>   digest.
> - `CheckpointIdentity.gate_binding_digest` is required with no model default.
>   `build_identity` also requires it.
> - Schema version 2 is enforced. A v1 checkpoint cannot resume; absence of the new
>   required field also fails parsing closed.
> - `"in-tree"` cannot collide with a real SHA-256 digest because it is not a 64-character
>   hexadecimal value.
> - `Checkpointer.write()` and `load_and_verify()` both use `self.gate_binding_digest` in
>   the same recomputed identity checked by `verify_identity`.
> - Production `_build_checkpointer()` explicitly supplies the binding. Its constructor
>   default only preserves existing in-tree callers and cannot make an external checkpoint
>   pass: omission on external resume recomputes `"in-tree"` and produces a mismatch.
> - Fresh external flow validates both the external gate and run directory as outside the
>   Git tree, then freezes the gate before constructing the checkpointer.
> - A supplied in-tree `.claude/verify.sh` cannot override an external resume: the frozen
>   path is passed into the orchestrator whenever it exists.
> - The new downgrade, tamper, and positive tests use actual `Checkpointer.write()` and
>   `load_and_verify()` against a `FakeStore`. The positive case requires a real stored
>   checkpoint and matching identity, so the prior `SystemExit(1)` false positive is gone.
>
> I found no residual downgrade path or regression in the reviewed scope.

---

**Disposition: RATIFIED at round 3** (0 BLOCKING / 0 MAJOR / 0 MINOR / 0 NIT). The gate is
external, immutable, and identity-bound; a resume cannot downgrade or swap it without a
detectable checkpoint-identity mismatch.
