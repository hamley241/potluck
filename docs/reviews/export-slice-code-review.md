# The export slice — review findings (3 rounds, declared cap 3)

Reviewer `codex` (OpenAI, `gpt-6.1-sol` — verified per round from the CLI's session
rollouts, not inherited), doer `claude` (Anthropic). Rounds 1 and 2 reviewed the uncommitted
working tree; round 3 reviewed `26af2857442d8832702278751abacbd2abd6fee4`.

codex could not run these suites in any round: its sandbox is read-only and blocks the
temporary-repository fixtures and uv lock creation. Static review by codex, deterministic gate
by the doer — the standing arrangement prior receipts record. Round 3 says so explicitly.

## Round 1 — verdict: CHANGES (3 MAJOR, 1 MINOR)

I’ll read the ruling and acceptance criteria first, then trace the export paths and tests independently before assessing your four concerns.

The normal export path skips `deliver()`, and the extraction appears to preserve ordinary delivery behavior. I found a possible isolation hole in the filesystem writes and am checking the loader’s validation boundaries. The tests cover normal pass, failure and HEAD-refusal paths, but do not exercise partial publication failures.

Round 1 of the declared 3. I found three MAJOR issues and one MINOR verification gap.

1. **MAJOR — Export can overwrite the operator’s files through a pre-existing temporary-file symlink.**  
   [_write_atomic](/Users/gp/development/autos/potluck/harness/export.py:52) writes to a predictable `<payload>.tmp` using `Path.write_bytes()`, which follows symlinks. An out-of-tree export directory containing `cumulative.patch.tmp → <operator checkout>/tracked-file` passes directory validation. Export then truncates that operator file before replacing the symlink with the payload filename. The run can even report successful export afterward.

   This breaks the central isolation contract despite never calling `deliver()`. Create temporary files exclusively without following existing links, and add a fixture verifying the complete operator state after this refusal.

2. **MAJOR — `load_export()` accepts incomplete transport metadata and unverified station bindings.**  
   [The loader](/Users/gp/development/autos/potluck/harness/export.py:123) checks task, versions, payload hashes and two redundant digests, but accepts manifests missing `run_id`, round, repository, base, output identities and `imported_result_digest`. It never validates `station-run.json` or checks its base, output digest and input bindings against the manifest and payloads.

   I confirmed with an in-memory fixture that it accepts a manifest missing all those transport fields whose five payloads contain the literal bytes `not a station artifact`. It also accepts `schema_version: true` because Python treats `True == 1`; a top-level JSON array raises `AttributeError` instead of a named `ExportError`.

   A later importer treating this function’s “accepted” result as verified would lack the promised provenance checks. Validate the manifest’s required types and fields, parse the station artifact, and enforce the cross-file agreements. Tree reconstruction itself can remain part 2.

3. **MAJOR — Repository identity cannot distinguish the expected repository from a fork.**  
   [The identity construction](/Users/gp/development/autos/potluck/harness/cli.py:846) records basename plus root commits. Two forks checked out under the same basename share this identity, even when they have different canonical push destinations. A delivery implementer comparing this field could therefore accept the wrong repository.

   Preserve the canonical repository identity needed for the later expected-repository check. Root commits can supplement it, but cannot establish ownership or destination.

4. **MINOR — The tests do not prove publication safety under interrupted payload writes or genuine directory reuse.**  
   [The tests](/Users/gp/development/autos/potluck/harness/test_export.py:99) delete a completed manifest and test an unwritable directory; neither injects failure after some payloads have been replaced. The stale-manifest fixture starts with `{}`, which was never an acceptable earlier export.

   Add a valid earlier export, reuse its directory, and inject failures between payload writes and during manifest publication. Require a named refusal and unchanged operator state. The CLI’s early clearing looks correct for sequential reuse, but the requested failure property is not demonstrated.

Both acceptance amendments are **improvements, not evasions**. A6 restores the ruling’s explicit task identity. A11 makes export failure observable while preserving the station’s separate verdict; the implementation’s JSON, human summary and exit-code branches reflect that distinction.

Your concerns, assessed separately:

- **Random run ID:** a provenance weakness, but not independently a contract violation. The exported station bytes and their digest provide correlation, and the existing checkpoint manifest ID deliberately identifies equivalent inputs rather than a unique invocation. Documenting or recording their relationship would help; randomness does not foreclose round chaining by manifest digest.
- **Repository identity:** real; finding 3.
- **Private helpers:** no independent finding. I found no resulting behavioral defect.
- **`delivery_err` naming:** no independent finding. It is confusing internally, but export failures are exposed as export failures to callers.

`station-run.json` v1 remains unchanged in producer code, shape and meaning. The `change()` extraction preserves the ordinary delivery algorithm and existing HEAD-refusal text. I found no foreclosure of import, input-aware restore/resume, or the deferred delivery-message repair.

Validation was read-only: source review and in-memory loader probes. The filesystem restriction prevented running the temporary-repository suites, so I cannot attest A1 or `ALL SUITES PASS`.

verdict: CHANGES
## Round 2 — verdict: CHANGES (1 MINOR, a regression introduced by the r1 fold)

I’ll check the four folds against the working tree and focused tests, keeping the review limited to those findings and any regressions they introduced.

The loader’s cross-check uses the right encoding: spec and acceptance are hashed as UTF-8 with replacement, and the gate is hashed as exact bytes. Recording the raw push URL also fits the proposed division, provided the consumer canonicalises it and refuses delivery when identity is absent or invalid.

I found a possible regression in error handling: `_write_atomic` now raises `ExportError` when it cannot clear a temp name, but `_export` does not catch that exception. I’m checking that path.

One fold introduced a MINOR regression:

- [`harness/cli.py:865`](/Users/gp/development/autos/potluck/harness/cli.py:865) does not catch `ex.ExportError`, which [`_write_atomic`](/Users/gp/development/autos/potluck/harness/export.py:73) now raises when unlinking a temp name fails—for example, if `cumulative.patch.tmp` is a directory. The exception escapes instead of producing the promised `export_failed` JSON and human summary. Confirmed with an in-memory fault injection. Catch `ex.ExportError` alongside the existing exceptions and cover this failure path.

The four original findings otherwise close:

1. Temp symlinks are removed safely; stale regular `.tmp` files are removed before exclusive creation. The module’s writes use newly created descriptors.
2. The requested field validation and artifact cross-checks are present. The encoding assumption is correct.
3. Recording `push_url_raw` is the right division. Part 3 should use the conductor’s single canonicaliser and refuse absent or invalid identity; export-time canonicalisation is unnecessary.
4. The reuse tests establish that payload or manifest publication failure leaves no acceptable earlier export.

Full filesystem tests could not run under this session’s read-only permissions.

verdict: CHANGES
## Round 3 — verdict: SHIP

I’ll review the commit, check the fold and regression test, and run the relevant validation. I’ll distinguish any blocking findings from follow-ups.


Reviewed `26af2857442d8832702278751abacbd2abd6fee4`. The fold closes the round 2 finding: `ex.ExportError` now reaches the existing failure path, preserving `station_outcome="passed"` while reporting `export_failed` and exiting 1.

The directory fixture exercises the actual exception source and checks the refusal, absence of a traceback, and worktree cleanup. I found no new blocking issues or nonblocking follow-ups.

I attempted `./test`, but this read-only sandbox prevents temporary-file and uv-lock creation. I could not independently reproduce the reported 722-check pass.

verdict: SHIP