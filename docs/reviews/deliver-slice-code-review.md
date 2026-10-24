# The delivery slice — review findings (4 rounds: a declared cap of 3, plus a declared overrun)

Reviewer `codex` (OpenAI, `gpt-6.1-sol`, verified per round from the CLI's session
rollouts), doer `claude` (Anthropic). codex ran no suite in any round: its sandbox is
read-only and blocks these fixtures. Static review by codex, deterministic gate by the doer.

## Round 1 — verdict: CHANGES (1 MAJOR, 1 MINOR)

I’ll check the amendment against the spec, then trace the delivery path and tests, focusing on index writes and whether refusals leave the repository untouched.

The amendment looks legitimate: the caller restriction keeps importing runs off ordinary delivery, and A3 preserves that path’s existing behavior. The new path also checks HEAD, the index, and the working-tree digest in the required order; the unrelated-edit test exercises the decisive refusal case.

I’m checking two remaining points: whether `git apply` guarantees atomicity beyond patch-check failures, and whether the tests compare the index’s actual bytes.

The A16/A17 narrowing is **legitimate**, not an evasion. A3 protects ordinary delivery, and importing runs now require export, so they cannot reach the old fallback. Removing that fallback would discard the plain run’s retained patch. One qualification: application failure alone does not prove the tree changed; filesystem errors can also cause it. That overstatement is inherited behavior protected by A3.

`GIT_OPTIONAL_LOCKS=0` is appropriate for suppressing opportunistic index refreshes; it is not a general write prohibition. In this implementation, explicit index-writing commands use temporary indices, and final application uses working-tree-only `git apply`. I found no command that intentionally writes the real index or a ref. [Git’s documentation](https://git-scm.com/docs/git#Documentation/git.txt-codeGITOPTIONALLOCKScode) supports that distinction.

I found **one MAJOR and one MINOR** against commit `9119edf`.

- **MAJOR — a write failure can leave partial delivery while reporting “nothing was written.”** At [harness/cli.py:1615](/Users/gp/development/autos/potluck/harness/cli.py:1615), every nonzero `git apply` exit becomes a `patch_conflict` refusal with an unchanged-tree assurance. Git checks patch applicability before writing, but its write phase is not transactional: it removes old files and creates replacements sequentially, returning on a write error without rolling earlier operations back. See Git’s `write_out_one_result` and `write_out_results` in [apply.c](https://github.com/git/git/blob/v2.50.1/apply.c).

  A clean checkout with a patch modifying two tracked files, the later one inside a readable but non-writable directory, can pass every baseline check and fail after earlier mutations. This violates A14/A15 and makes the refusal message false. The current conflict test exercises an obstruction detected before writes, so it cannot expose this failure. Add a write-phase failure fixture and restoration handling; never infer “nothing written” solely from the exit code. I did not execute this fixture in the read-only sandbox.

- **MINOR — the preservation assertions do not compare index bytes.** The new tests use `_operator_state`, whose index observation is `git ls-files -s` ([harness/test_export.py:51](/Users/gp/development/autos/potluck/harness/test_export.py:51)). That checks entries, not stat-cache data or index extensions. A metadata-only index rewrite would pass the assertions labelled “byte-identical.” The helper also runs ordinary `git status`, which can refresh the index itself. Capture the actual worktree-specific index bytes before and after delivery, before running potentially refreshing observations.

The remaining requested checks look sound by inspection:

- HEAD, real-index contents, then working-tree digest are checked in order before application. The unrelated-edit fixture explicitly establishes that the patch would apply, then requires baseline refusal.
- Raw push URL and root commits are compared; null URL refuses; checkout basename is excluded.
- Selection binds the manifest digest and rechecks the patch bytes actually used.
- Baseline and patch-conflict messages have distinct structured kinds, captured facts, and the retained export path.
- The “once” disclaimer explicitly covers empty patches leaving the baseline intact, retries, crashes, and competing results. It is honest.
- The core negative fixtures appear sensitive to removal of their corresponding safeguards. The atomicity and index-byte gaps above remain untested.

I did not rerun the gate; the reported 869 checks remain your execution evidence. The interface supports the conductor’s next slice, but the all-or-nothing assurance needs repair before that switch relies on it.

verdict: CHANGES
## Round 2 — verdict: CHANGES (1 MAJOR, 1 non-BLOCKING)

I’ll review the two folds, focusing on the baseline guarantee behind `clean -fd` and the shared test helper change.

The index fold looks sound, and disabling optional locks in the shared helper is appropriately scoped. I found a recovery hazard: `clean -fd` runs even if `checkout-index` fails. If the partial apply changed ignore rules, the failed restore can leave previously ignored operator files exposed to deletion.

One blocking issue remains in the MAJOR fold.

- **MAJOR — cleanup can delete previously ignored operator files after a failed restore.** At [cli.py:1647](/Users/gp/development/autos/potluck/harness/cli.py:1647), `clean -fd` runs regardless of whether `checkout-index` succeeded. Suppose the partial apply removes an ignore rule, then fails on a later file. If restoring `.gitignore` also fails, cleanup can delete operator files that were ignored at baseline. Those files legitimately passed the baseline check and need not overlap any patch path.

  The baseline premise holds for files under the **original ignore rules**; it does not establish safety under partially applied rules. Skip cleanup when checkout restoration fails, report incomplete recovery, and add a regression covering an ignore-rule change plus failed restoration.

- **MINOR — closed.** Hashing the actual index before other observations fixes the comparison. Setting `GIT_OPTIONAL_LOCKS=0` in the shared test helper is reasonable across the three suites; explicit writes still execute.

Non-blocking follow-up: update `_deliver_selected`’s docstring, which still promises “having written nothing” and “all-or-nothing.”

I reviewed the code and fixture; I could not independently rerun the gate in this read-only environment.

verdict: CHANGES
## Round 3 (the cap) — verdict: SHIP (1 non-BLOCKING)

I’ll review the diff and recovery tests, focusing on the cleanup guard, refusal message, and whether the injected failure models a reachable state.

The guard closes the cleanup finding: a failed `checkout-index` now skips `clean` and explains why. The injected failure models a reachable state—interruption or a filesystem write error can stop restoration before `.gitignore` is restored—even though this chmod fixture does not.

I found one non-blocking wording edge: `partial_application` also covers failed re-verification, where writes are explicitly unknown, but the new footer always says “Files WERE written.”

The folds close the MAJOR and docstring finding. No blocking regression found in `3927e92..b40af57`.

The injected scenario is reachable: interruption or a filesystem write failure can prevent `checkout-index` from restoring `.gitignore`. The chmod fixture’s successful restore does not establish that recovery always succeeds.

One **NON-BLOCKING follow-up** remains at [cli.py:1730](/Users/gp/development/autos/potluck/harness/cli.py:1730): `partial_application` also covers failed re-verification, where writes are UNKNOWN and no current digest is available. The footer nevertheless asserts “Files WERE written” and says the current digest appears above. Use wording that preserves that uncertainty.

`git diff --check` passed. I could not independently rerun the suites in this read-only environment; the 886-check result remains your reported validation.

verdict: SHIP
## Round 4 — DECLARED OVERRUN, reviewing one fold — verdict: SHIP (1 non-BLOCKING, FILED)

I’ll review the single fold against the r3 note and check for any blocking regression.

Reviewed `b40af57..9553e0965aeb60083e61ceb0d467d2a52c35dee6`. The footer fold closes my r3 note and introduces no blocking regression.

One non-blocking follow-up remains: `_REFUSAL_LABEL["partial_application"]` still says “git apply wrote some files,” including on the UNKNOWN path. The new test excludes “Files WERE written” but misses that header assertion.

I could not rerun tests in this read-only environment; the 890-check pass is your reported gate.

reviewed_head: `9553e0965aeb60083e61ceb0d467d2a52c35dee6`

verdict: SHIP