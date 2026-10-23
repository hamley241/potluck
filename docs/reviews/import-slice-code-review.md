# The import slice — review findings (2 rounds of a declared 3)

Reviewer `codex` (OpenAI, `gpt-6.1-sol`), doer `claude` (Anthropic). Round 1 reviewed
`3538fbb3a666fdde27a1ef2e2b446575555cbf10`; round 2 reviewed the folds in `53631a00cd6ab539ae23339853da86f4b6ad2420`.
codex ran no suite in either round: its sandbox is read-only and blocks the temporary-repository
fixtures. Static review by codex, deterministic gate by the doer.

## Round 1 — verdict: CHANGES (2 MAJOR)

I’ll read the ruling and slice requirements, then check the reconstructed CLI paths against the tests, focusing on writes before refusal and retry/resume behavior.

The base check is correctly placed before `git apply`, and the resume marker covers omission of `--import-dir`. I found a gap in patch verification: the importer rereads the patch after `load_export()` verifies it. I’m checking whether tree verification fully closes that gap. The filesystem is read-only, so I can’t run the suites that create temporary repositories.

Round 1 of 3. Two MAJOR findings; no BLOCKING, MINOR, or NIT findings.

1. **MAJOR — the applied patch bytes are not necessarily the verified bytes.** [cli.py:962](/Users/gp/development/autos/potluck/harness/cli.py:962)

   `_load_import()` calls `load_export()`, then rereads `cumulative.patch`. A replacement between those reads bypasses the declared patch digest check. The comment’s tree-digest argument is insufficient: a substituted patch can produce the attested tree **plus an ignored file**, which the tree digest excludes but the doer or gate can consume. Restore also reapplies those replacement bytes.

   An in-memory probe confirmed `_load_import()` returns replacement bytes whose SHA-256 differs from the accepted manifest. Return the verified payload bytes from the loader, or verify the captured bytes against the accepted manifest before applying them. Add a substitution test; the current corruption test only covers corruption before loading.

2. **MAJOR — disjoint run-directory paths can still overwrite the import through child symlinks.** [cli.py:1160](/Users/gp/development/autos/potluck/harness/cli.py:1160)

   For example, pre-create `run1/imported-result.json` as a symlink to `export0/station-run.json`. The run and import directories pass `_overlaps()`, but `marker.write_text()` follows the link and truncates the accepted input artifact before the doer runs.

   Protect actual write destinations, including temporary files, against links into the import before writing. The [overlap test](/Users/gp/development/autos/potluck/harness/test_import.py:304) exercises only equal and nested directory paths, so it misses this destructive case.

The other requested paths look correct on inspection:

- Checking `self.base != want_base` before apply correctly catches an empty commit moving HEAD.
- Import restoration remains wired, resets and reapplies the imported baseline; `--allow-dirty` still drops restoration.
- The marker refuses resume even when `--import-dir` is omitted.
- Chaining uses the imported manifest digest; non-importing exports retain `null`.
- Station-run v1 and starting-HEAD semantics remain unchanged; no synthetic baseline commit appears.
- Nothing identified forecloses part 3.

`--import-dir` without `--export-dir` is not a cumulative-redelivery finding here. Part 3 should explicitly define its delivery behavior and test that the complete candidate is delivered once through the manifest-selected delivery boundary.

Validation was limited to source inspection and an in-memory probe: the read-only sandbox prevented running the repository-writing suites.

verdict: CHANGES
## Round 2 — verdict: SHIP (one non-BLOCKING follow-up declared)

I’ll inspect both folds and the write paths under `--run-dir`, then check whether the regressions cover the reported failures and any remaining link exposure.

The digest check now covers the captured bytes that are applied and retained for retries. The preflight scan also catches planted links at the gate, checkpoint, and run-input temporary filenames: those writes use immediate children of the resolved run directory. I’m checking the remaining output handling for gaps.

Both folds close their findings. No new blocking problem found.

- **MAJOR 1 closed:** `_load_import` hashes the captured patch bytes against the accepted manifest before applying them. Those same bytes feed retry restoration. The refusal names both digests.
- **MAJOR 2 closed:** the preflight scan catches existing symlinks into the import, including the temporary filenames used by `_freeze_gate`, checkpoint writes, `persist_run_inputs`, and station-run writes. These are immediate children of the resolved run directory, so the scan’s lack of traversal through nested directory symlinks leaves no bypass in those paths. The marker additionally has its own no-follow protection.
- The test-robustness fix correctly records an invalidated export as a failed check.

`_refuse_links_into_import` is the right shape for **pre-existing symlinks**. A link installed after the scan could still be followed by the ordinary temporary-file writes. That is a real, **non-blocking hardening follow-up** for concurrent destination mutation; use no-follow temporary-file creation consistently to close it.

Review was static: this session’s read-only filesystem prevented rerunning the write-dependent suites. The reported 793-check gate is not independently verified here.

verdict: SHIP