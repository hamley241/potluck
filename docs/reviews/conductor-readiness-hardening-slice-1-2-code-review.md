# conductor-readiness hardening slice §1+§2 — codex code-review findings (verbatim)

Independent, cross-provider CODE review of the coupled §1 (step-adaptive doer
timeouts + `--doer-timeout` + outer-bound call-budget) and §2 (typed ExecCursor +
atomic checkpoints + potluck lossless git-tree resume + menu debate-state resume +
backoff) hardening slice, by **codex (OpenAI), model `gpt-5.6-sol`** — read-only
sandbox (`codex exec -s read-only`), doer Claude (Anthropic). Review ran over BOTH
repos' recorded OID ranges each round (potluck `c2a1d01..HEAD`, menu
`73c4f46..HEAD`), the full `git diff` of the range — both potluck reviewed surfaces
(engine-adjacent `harness/` + the CLI) and menu are in the reviewed bytes
(obs-018/obs-021). `harness_core` is untouched in both ranges (the FENCE held).

Twelve rounds (r1→r12). Every round's verdict is reproduced VERBATIM below so the
ratification is verifiable, not asserted. Each round I folded every in-scope
finding, re-ran both full suites green, committed, and re-reviewed the updated
range. The convergence tightened from behavioural checkpoint-window defects
(rounds 1–8) to config/validation gaps (round 9) to tamper/corruption fail-closed
paths (rounds 10–11), reaching zero findings at round 12.

The §5 `N_max` live-run measurement is an ACCEPTED obs-017 declared gap (decider
ruling (b)): the pins are stub-measured pending §5's live-doer justification;
worst-case blast radius is a premature, resumable `ESCALATED_CALL_BUDGET`. codex
was told each round to treat it as note-only; it was not re-raised as blocking.

---

## Round 1 — verbatim

> ## BLOCKING
>
> - `potluck/harness/orchestrator.py:1478` and `menu/harness/orchestrator.py:578` — resume exceeds the ratified retry ceiling and invalidates `C_max`. With `MAX_RESUME_RETRIES=1`, the original call times out, then resume executes `1 + R = 2` additional attempts. Total: three calls and one delay, while §1.1 budgets `timeout × (1 + R) + delay × R`, i.e. only two calls total. A call consuming its full timeout on each attempt can therefore exceed the validated outer bound. The tests explicitly encode this wrong interpretation (`menu/harness/test_resume.py:178-183`: `R=2` means three resume attempts, excluding the original failure).
>
> - `potluck/harness/orchestrator.py:1456-1467` and `menu/harness/orchestrator.py:558-568` — checkpoints are written only at the head of calls, not after successful completion. There are also no `Phase.DONE` writes. Inputs: a model call succeeds, then the process dies before the next call’s head checkpoint. Result: resume uses the pre-call cursor/state; menu re-litigates the completed call, while potluck can restore the pre-call tree and discard completed doer work. This violates §2.0/§2.1’s before-and-after program counter and the invariant “a run may lose a call, never its history.”
>
> ## MAJOR
>
> - `potluck/harness/orchestrator.py:2337-2362` and `menu/harness/orchestrator.py:1465-1476` — tiebreak timeouts are not resumable timeouts. They are converted into an unresolved concern and ultimately `ESCALATED_DISAGREEMENT`, despite `TIEBREAK` being a ratified resumable phase. Inputs: a tiebreaker call times out. Wrong behavior: no `ESCALATED_TIMEOUT`, no same-step resume/backoff, and the timeout is treated as substantive disagreement.
>
> - `potluck/harness/orchestrator.py:1755-1762` and `menu/harness/orchestrator.py:1289-1298` — one `TIEBREAK` cursor covers a loop containing potentially many calls, with no concern ID or per-call position. Inputs: concern C1 is adjudicated successfully and C2 times out/process crashes. Resume re-enters the entire `_tiebreak` loop, re-adjudicating C1 and potentially duplicating ledger/tiebreak side effects. This is not exact failed-call dispatch.
>
> - `potluck/harness/orchestrator.py:199-215` and `menu/harness/orchestrator.py:168-181` — the `N_max` counter resets to zero whenever a resume constructs a new orchestrator, and `calls_dispatched` is absent from both checkpoint schemas. Inputs: an invocation dispatches `N_max` calls and checkpoints; resume starts another invocation. Result: another `N_max` calls can be admitted for the same logical run, contradicting §1.3’s total-call invariant and permitting repeated resumes to evade the bound.
>
> ## MINOR
>
> - `potluck/harness/checkpoint.py:501-513` — the claimed recomputation of potluck’s tree commitments is partly self-comparison: `repo_base_commit` and `tree_snapshot_digest` are copied directly from `ckpt.identity` into the “recomputed” identity. The later restore independently checks HEAD and materialized tree content, so this is not by itself the primary failure, but `load_and_verify()` does not perform the recomputation its contract claims.
>
> - The `N_max` pins are described in comments as measured against a “real” run, but the committed budget tests use injected/stub clients. I could not verify from the reviewed code or tracked evidence that a live-provider run was measured and remained within the pins.
>
> ## NIT
>
> - None.
>
> The reviewed OID ranges do not modify `harness_core`, and I found no deployment, publication, or spending action.
>
> VERDICT: `CHANGES REQUIRED` — fix post-success checkpointing, retry arithmetic, resumable/per-concern tiebreak dispatch, and persisted cumulative call-budget admission.

---

## Round 2 — verbatim

> ## BLOCKING
>
> - [potluck/harness/orchestrator.py:2436](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:2436) and [menu/harness/orchestrator.py:1564](/Users/gp/workspace/masala/menu/harness/orchestrator.py:1564) — per-concern tiebreak calls still lack an immediate DONE checkpoint. Both implementations record `tiebreak_decided` only in the next concern’s HEAD checkpoint or the loop-level DONE checkpoint. Failure scenario: concern C1 adjudication succeeds → process crashes before the next checkpoint → resume restores C1 as undecided and dispatches it again. Worse, if that success was the allowed resume retry, the restored cursor still has the exhausted attempt count, so `_run_step` raises `resume_exhausted` and discards a completed adjudication. This violates “HEAD before + DONE after every resumable call,” the no-lost-history invariant, and the round-1 MAJOR-2 correction. Potluck explicitly documents the unsafe window at [potluck/harness/orchestrator.py:2477](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:2477).
>
> ## MAJOR
>
> - [potluck/harness/orchestrator.py:1493](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:1493) with [potluck/harness/orchestrator.py:224](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:224), and [menu/harness/orchestrator.py:586](/Users/gp/workspace/masala/menu/harness/orchestrator.py:586) with [menu/harness/orchestrator.py:183](/Users/gp/workspace/masala/menu/harness/orchestrator.py:183) — `calls_dispatched` is checkpointed before admission increments it. A timed-out call therefore never appears in the persisted cumulative count. Failure scenario: checkpoint stores count 10 → call 11 is admitted and times out → resume restores 10 → another call is admitted as 11. Repeating this at different phases permits more than `N_max` actual dispatches across the logical run. The `(N_max+1)`th real dispatch is not necessarily refused, invalidating the enforced premise behind `outer_min = C_max × N_max`. Round-1 MAJOR-5 is papered over, not resolved.
>
> - [potluck/harness/config.py:131](/Users/gp/workspace/masala/potluck/harness/config.py:131), [potluck/harness/test_call_budget.py:34](/Users/gp/workspace/masala/potluck/harness/test_call_budget.py:34), [menu/harness/config.py:121](/Users/gp/workspace/masala/menu/harness/config.py:121), and [menu/harness/test_config_hardening.py:100](/Users/gp/workspace/masala/menu/harness/test_config_hardening.py:100) — the required real-run justification for the `N_max` pins is not present. Potluck expressly says its count was measured under stubs; its test’s “real end-to-end run” is `_heavy_orch` composed entirely of stub providers. Menu likewise checks a stubbed orchestrator. No `test_live_doer.py` exists in either repository, and no full live run evidence establishes that either station remains within its pin. This claim cannot be verified from the code.
>
> ## MINOR
>
> None.
>
> ## NIT
>
> None.
>
> The retry arithmetic, completed-flag phase dispatch, timeout-to-`TimeoutEscalation` mapping, persisted menu tiebreak verdicts, timeout precedence, potluck tree restoration guards, run-directory refusal, and `harness_core` scope fence appear resolved.
>
> VERDICT: `CHANGES REQUIRED` — missing per-concern tiebreak DONE checkpoints; cumulative call-budget undercount across timeouts/resumes; no real-run `N_max` justification.

---

## Round 3 — verbatim

> ## BLOCKING
>
> - [menu/harness/orchestrator.py:1593](/Users/gp/workspace/masala/menu/harness/orchestrator.py:1593) — The per-concern POST checkpoint is written before applying the adjudication’s semantic effects at lines 1597–1630. Inputs: a factual tiebreak sides with the doer, either for an ordinary concern or an accepted-open-risk concern; the POST write persists `tiebreak_decided[cid] = True` and the verdict; the process crashes immediately afterward. On resume, line 1531 skips that concern, so `standing_open_risk.add(cid)` or `_set_entry(...REBUTTED_CONCEDED...)` never happens. The ledger remains HELD and the run can incorrectly escalate, omit the required risk-register obligation, or produce the wrong terminal state. The round-3 fold is therefore incomplete: it preserves the verdict but loses the completed adjudication’s required side effects. The POST checkpoint must follow all concern-specific state and ledger mutations.
>
> ## MAJOR
>
> - [menu/harness/orchestrator.py:578](/Users/gp/workspace/masala/menu/harness/orchestrator.py:578) and [potluck/harness/orchestrator.py:1484](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:1484) — HEAD checkpoints blindly persist `calls_dispatched + 1` before either call-budget admission or retry-exhaustion validation. Inputs: the restored count is already `N_max`, or a failed cursor has `attempt == MAX_RESUME_RETRIES`; the call site writes its HEAD checkpoint first, then `BudgetedStepRunner.run()` refuses admission or `_run_step()` re-escalates without dispatch. The checkpoint nevertheless records a nonexistent dispatch—possibly `N_max + 1`. Repeated exhausted resumes keep increasing the persisted “dispatched” count without making calls. This contradicts the folded requirement that `calls_dispatched` count actual in-flight/dispatched attempts and never exceed `N_max`. Admission/retry eligibility must be established before committing the increment, while still ensuring an admitted timeout is persisted.
>
> ## MINOR
>
> - None.
>
> ## NIT
>
> - None.
>
> The declared §5 live-provider measurement gap was treated as accepted. `harness_core` remains at `c5d4058`; no floor act was found. I could not execute the resume suites because the read-only environment provides no writable temporary directory, so findings are based on direct control-flow inspection.
>
> VERDICT: `CHANGES REQUIRED` — menu tiebreak POST checkpoints lose adjudication side effects; both repos persist nonexistent/out-of-budget dispatches.

---

## Round 4 — verbatim

> ## BLOCKING
>
> - [menu/harness/orchestrator.py:622](/Users/gp/workspace/masala/menu/harness/orchestrator.py:622) and [potluck/harness/orchestrator.py:1535](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:1535) — `calls_dispatched` is persisted after admission only when `TimeoutEscalation` is caught. Scenario: HEAD checkpoint records count `k`; runner admits and dispatches call `k+1`; the call succeeds, but the process crashes before the DONE checkpoint. Resume restores `k` and dispatches the call again, yielding `k+2` actual dispatches while the counter says `k+1`. Repeating this window can dispatch more than `N_max`, invalidating the binding `C_max × N_max` outer bound. The same undercount occurs when a dispatched call raises a non-timeout error before DONE. Admission must persist the increment after successful budget admission and before provider dispatch; timeout-only bumping is insufficient.
>
> ## MAJOR
>
> None.
>
> ## MINOR
>
> None.
>
> ## NIT
>
> None.
>
> Round-3 fold verification: menu’s tiebreak POST checkpoint now occurs after both `standing_open_risk` and `REBUTTED_CONCEDED` side effects, so I found no remaining skipped-ledger-obligation path. Refused calls and `resume_exhausted` calls are correctly not counted; timed-out attempts are counted. The new defect is the successful/error dispatch crash window above.
>
> The declared §5 live-run/N_max gap remains accepted as instructed. `harness_core` is untouched. I could not execute the targeted suites because this read-only environment provides neither `pytest` nor a writable temporary directory; the verdict is based on direct code tracing.
>
> VERDICT: `CHANGES REQUIRED` — fix cumulative call-count underreporting between admission and the timeout/DONE checkpoint.

---

## Round 5 — verbatim

> ## BLOCKING
>
> None.
>
> ## MAJOR
>
> - [potluck/harness/orchestrator.py:2482](/Users/gp/workspace/masala/potluck/harness/orchestrator.py:2482) applies `resolve_doer_timeout()` to `tiebreaker:adjudicate`. Input: `potluck fix --doer-timeout 600` followed by a deadlock requiring tiebreaking. Wrong behavior: the tiebreaker receives 600 seconds, although §1.1 defines the flag as a uniform override for doer steps only and requires reviewer/tiebreak calls to retain the 300-second default. Conversely, `--doer-timeout 0` makes tiebreaking immediately time out. This expands the CLI flag beyond its ratified role boundary.
>
> - [potluck/harness/config.py:94](/Users/gp/workspace/masala/potluck/harness/config.py:94) retains every built-in doer-map value in `C_max` even when the uniform override replaces that map. Input: default `N_max=30`, `--doer-timeout 100`, and `feature_seconds=18150`, which exactly covers the actual maximum base of 300 seconds with retry/backoff: `(300×2+5)×30`. Wrong behavior: `max_call_base()` still includes the dormant 600-second map entries, computes `outer_min=36150`, and `validate()` refuses a configuration satisfying the ratified effective-timeout bound. Override precedence must replace the map when calculating `C_max`, not merely append another candidate.
>
> ## MINOR
>
> None.
>
> ## NIT
>
> - Automated tests could not be executed because `pytest` is not installed in the review environment. The findings above are directly established by the call sites and arithmetic; diff whitespace checks passed.
>
> The round-4 `on_admit` change itself is sound: successful admissions are persisted before provider dispatch; budget-refused and retry-exhausted calls are not counted; restored counts prevent cumulative dispatches exceeding `N_max`.
>
> VERDICT: `CHANGES REQUIRED` — potluck applies `--doer-timeout` to the tiebreaker and calculates `C_max` from overridden, inactive map values.

---

## Round 6 — verbatim

> BLOCKING
>
> None.
>
> MAJOR
>
> - [menu/harness/orchestrator.py:1350](/Users/gp/workspace/masala/menu/harness/orchestrator.py:1350) — The completed `ROUND_FOLLOWUP` checkpoint is written before the round summary is constructed. `round_concessions` is only a local list initialized at line 1256 and is not checkpointed. Scenario: reviewer concedes concern `C1` → ledger records `REBUTTED_CONCEDED` and `_done(ROUND_FOLLOWUP)` persists a completed cursor → process crashes before lines 1352–1360 → resume skips the completed follow-up → `round_concessions` is reinitialized empty → the resulting `RoundSummary.concessions` and `concerns_closed` omit `C1`. The ledger and round history then disagree, violating lossless debate-state resume. Move the completed checkpoint after round finalization, or persist/derive the pending round summary state.
>
> - [potluck/harness/cli.py:139](/Users/gp/workspace/masala/potluck/harness/cli.py:139) — Run-directory containment is checked against `Path.cwd()`, not the Git working-tree root. Scenario: Git root is `/repo`, invocation is from `/repo/project` with its own `.claude/verify.sh`, and `--run-dir ../runs/x` resolves to `/repo/runs/x`. The directory is inside the Git working tree but outside the current directory, so the CLI accepts it. Subsequent Git-wide snapshot/restore operations can capture or destroy the checkpoint sidecar, exactly what §2.3 requires the CLI to refuse. Resolve the root using `git rev-parse --show-toplevel` and compare against that canonical path.
>
> MINOR
>
> None.
>
> NIT
>
> - The declared §5 live-provider justification gap remains accepted and note-only as instructed.
> - I could not execute the focused suites because `pytest` is unavailable in this environment. Static inspection confirmed the round-5 corrections: potluck tiebreak uses `model_call_seconds`, `max_call_base()` replaces the dormant doer map when overridden, and implement/apply-fixes/respond still resolve through the doer override.
>
> VERDICT: `CHANGES REQUIRED` — menu loses round-concession history across a completed-follow-up crash window; potluck does not reliably refuse run directories inside the Git working tree.

---

## Round 7 — verbatim

> ## BLOCKING
>
> None.
>
> ## MAJOR
>
> - [potluck/harness/cli.py:171](/Users/gp/workspace/masala/potluck/harness/cli.py:171) constructs `GitCheckpointStore` with `repo=Path.cwd()` instead of the working-tree root already resolved at lines 145–152. Consequently [potluck/harness/checkpoint.py:102](/Users/gp/workspace/masala/potluck/harness/checkpoint.py:102) scans only that subtree for nested `.git`, and restore operations such as `git ls-files`, file removal, and `git clean` are likewise scoped from the invocation directory. Failure scenario: invoke `potluck fix` from `repo/subdir` with a nested repository or D/F transition in `repo/sibling`. The round-6 run-dir check correctly recognizes the whole repository, but checkpointing misses the sibling nested repo and can snapshot it as a gitlink, violating the mandatory refusal/losslessness contract; restoration also does not reliably empty sibling paths before materialization, so a sibling D/F transition can fail instead of resuming losslessly. Resolve `--show-toplevel` once and pass that root to `GitCheckpointStore`.
>
> ## MINOR
>
> None.
>
> ## NIT
>
> - The accepted obs-017 §5 live-run/N_max evidence gap remains note-only as directed.
> - The targeted test command could not be executed as written because this environment has `python3` but no `python` command; the finding above follows directly from live Git path scoping and the production wiring.
>
> The two round-6 folds themselves are present: menu writes the completed-round checkpoint after `RoundSummary` and ledger persistence, advances completed cursors to `round_index + 1`, and keeps non-completed cursors in the same round; potluck’s run-dir containment check uses `git rev-parse --show-toplevel`.
>
> VERDICT: `CHANGES REQUIRED` — potluck checkpoint/snapshot/restore remains incorrectly rooted at the invocation cwd rather than the Git working-tree root.

---

## Round 8 — verbatim

> BLOCKING
>
> None.
>
> MAJOR
>
> - [menu/harness/orchestrator.py:1303](/Users/gp/workspace/masala/menu/harness/orchestrator.py:1303) — successful `ROUND_RESPOND` calls are no longer checkpointed as completed. The next durable write occurs at the `ROUND_FOLLOWUP` head on line 1317, or at the end-of-round write on line 1373. Failure scenario: `doer:respond` succeeds and its state mutations/logging finish → process crashes after line 1305 but before either later checkpoint → the surviving cursor is the pre-call `ROUND_RESPOND` checkpoint → resume applies backoff and invokes `doer:respond` again. This re-litigates a completed call, may produce a different stance, and violates §2.0/§2.2’s before-and-after checkpoint contract and “a run may lose a call, never its history.” Moving the sole DONE write to after `RoundSummary` fixed the round-summary crash window but opened this earlier response-completion window. A distinct post-response checkpoint must durably preserve the response and its side effects without incorrectly marking the entire round complete.
>
> MINOR
>
> None.
>
> NIT
>
> None.
>
> The round-7 potluck fix itself is complete: both run-directory containment and `GitCheckpointStore` use `_git_toplevel()`, so snapshot, restore, clean, canonical-index checks, and nested-repository refusal operate over the whole working tree when invoked from a subdirectory.
>
> The §5 live-provider N_max evidence remains the accepted obs-017 gap and is note-only. `harness_core` is untouched, and no floor action appears in either range. I could inspect the live trees and diffs but could not independently execute their write-dependent test suites in the read-only environment.
>
> VERDICT: `CHANGES REQUIRED` — menu lacks a durable completed `ROUND_RESPOND` checkpoint before the next crash window.

---

## Round 9 — verbatim

> BLOCKING
>
> None.
>
> MAJOR
>
> - [potluck/harness/cli.py:62](/Users/gp/workspace/masala/potluck/harness/cli.py:62) — `--max-rounds` is applied after `HarnessConfig.load()` has validated the configuration, but `_apply_timeout_overrides()` revalidates only when `--gate-timeout` or `--doer-timeout` is present ([cli.py:279](/Users/gp/workspace/masala/potluck/harness/cli.py:279)). Scenario: `potluck fix --max-rounds 100` with the default 36,150-second feature bound raises `N_max` from 30 to 814 and `outer_min` to 980,870 seconds, yet starts without refusal. The outer timeout can therefore cancel the run long before the enforced `C_max × N_max` bound. This violates §1.2/§1.3’s mandatory post-override validation.
>
> - [potluck/harness/config.py:375](/Users/gp/workspace/masala/potluck/harness/config.py:375), [menu/harness/config.py:334](/Users/gp/workspace/masala/menu/harness/config.py:334) — both validators exempt an outer timeout of zero from `outer >= outer_min`. Scenario: a profile sets `feature_seconds = 0`, or menu sets `MENU_RUN_SECONDS=0`; validation succeeds, then the outer `wait_for` fires immediately before any step-level timeout or call-budget escalation can occur. The ratified design requires `feature_seconds/run_seconds >= outer_min` without this escape hatch, specifically so finer resumable signals cannot be preempted.
>
> MINOR
>
> None.
>
> NIT
>
> - The accepted obs-017 §5 live-run justification gap remains explicitly declared; per the Round 9 ruling, it does not affect the verdict.
> - The Round 9 menu repair itself appears correct: a completed `ROUND_RESPOND` resumes the same round at followup, only a completed `ROUND_FOLLOWUP` advances to the next round, and the latter checkpoint is written after the `RoundSummary` and ledger. I found no duplicated or lost summary at those crash boundaries.
> - `harness_core` remains at `c5d4058` with no reported working-tree changes, and neither reviewed range includes floor actions.
>
> VERDICT: `CHANGES REQUIRED` — potluck’s unvalidated `--max-rounds` override and both repositories’ zero-valued outer-bound bypass.

---

## Round 10 — verbatim

> ## BLOCKING
>
> - `potluck/harness/checkpoint.py:529-534` — potluck does not actually bind the restored `TreeSnapshot` to `CheckpointIdentity`. `load_and_verify()` constructs the “recomputed” identity using `ckpt.identity.repo_base_commit` and `ckpt.identity.tree_snapshot_digest` themselves. Restore then independently trusts `ckpt.tree` (`potluck/harness/checkpoint.py:537-538`) without checking that `ckpt.tree.repo_base_commit/tree_snapshot_digest` equal the identity fields. Failure: alter or corrupt only `checkpoint.json.tree` to another internally valid base/tree/OID/digest set while leaving `identity` unchanged → identity verification passes → `restore_tree()` verifies and restores the substituted tree. This directly violates §2.1’s recomputed base/tree identity and Ruling A’s fail-closed lossless resume.
>
> ## MAJOR
>
> - `potluck/harness/checkpoint.py:234-247` — the clean-tree precondition ignores ignored files, but restoration subsequently deletes them with `git clean -fdx`. Failure: checkpoint contains ignored `.env`; after timeout, the operator changes `.env`; `git status --porcelain` remains empty → resume considers the tree clean → `_empty_worktree_keep_index()` deletes the changed `.env` and restores the checkpoint version. Resume therefore overwrites post-timeout work rather than refusing a divergent tree, contradicting §2.3’s “nothing uncommitted is lost” precondition and fail-closed restore contract.
>
> - `potluck/harness/schemas.py:188` and `menu/harness/schemas.py:1147` — `calls_dispatched` accepts negative values, and both runners restore it without range validation (`potluck/harness/orchestrator.py:225-229`, `menu/harness/orchestrator.py:182-186`). Failure: a corrupted checkpoint with `calls_dispatched=-1` passes parsing and identity verification → the resumed logical run can dispatch `N_max+1` calls before refusal. This breaks the enforced `≤ N_max` premise underlying `outer_min = C_max × N_max`.
>
> ## MINOR
>
> - `potluck/harness/config.py:346-378` and `menu/harness/config.py:317-337` — neither validator validates `debate.max_rounds`. Failure: `potluck fix --max-rounds -100` or `MENU_MAX_ROUNDS=-100` produces a negative `N_max`, makes `outer_min` negative, and passes the supposedly unconditional outer-bound validation; the runner then refuses the first call under a nonsensical negative budget. Round-9 added revalidation, but the value being revalidated remains invalidly representable.
>
> ## NIT
>
> - None.
>
> The round-9 folds are present: potluck always revalidates after CLI overrides at `potluck/harness/cli.py:269-289`, and both outer-bound inequalities are unconditional at `potluck/harness/config.py:377-378` and `menu/harness/config.py:336-337`. `MENU_MAX_ROUNDS` is applied before `load()` validation. No `harness_core` files appear in either reviewed range. The accepted obs-017 live-run justification gap remains note-only.
>
> VERDICT: `CHANGES REQUIRED` — fix the unbound potluck tree identity, ignored-file destructive restore, and unvalidated cumulative call counter.

---

## Round 11 — verbatim

> BLOCKING
>
> - [potluck/harness/checkpoint.py:554](/Users/gp/workspace/masala/potluck/harness/checkpoint.py:554) — Round-10’s tree-binding fix is incomplete. `load_and_verify()` binds only `repo_base_commit` and `tree_snapshot_digest`; that digest covers only `work_tree_oid`. Neither `index_tree_oid` nor the complete `TreeSnapshot` is committed by the identity ([schemas.py:153](/Users/gp/workspace/masala/potluck/harness/schemas.py:153)).  
>   Failure scenario: replace `ckpt.tree.index_tree_oid` with another valid tree OID while retaining the original base, working-tree OID, and digest. Identity verification passes; `restore_tree()` installs and verifies the attacker-selected real index, so resume proceeds with staged state different from the checkpointed run. This violates the binding requirement and lossless dual-tree resume. The added test only mutates base/digest ([test_resume.py:627](/Users/gp/workspace/masala/potluck/harness/test_resume.py:627)), so it misses this case.
>
> MAJOR
>
> - None beyond the blocking defect above.
>
> MINOR
>
> - None newly identified.
>
> NIT
>
> - None.
>
> The other three Round-10 folds are correctly present in both relevant repos: ignored-file restore refusal, non-negative `calls_dispatched` plus defensive `restore_count()`, and source validation of negative `debate.max_rounds`. The accepted §5 live-run gap is not re-raised. `harness_core` is untouched in the reviewed ranges.
>
> VERDICT: `CHANGES REQUIRED` — bind the complete potluck tree snapshot, including `index_tree_oid`, into checkpoint identity verification.

---

## Round 12 — verbatim

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
> Round-12 verification: [potluck/harness/checkpoint.py:207](/Users/gp/workspace/masala/potluck/harness/checkpoint.py:207) binds all four `TreeSnapshot` fields using the specified NUL-delimited SHA-256. The identical helper is used when writing at [checkpoint.py:527](/Users/gp/workspace/masala/potluck/harness/checkpoint.py:527) and verifying at [checkpoint.py:572](/Users/gp/workspace/masala/potluck/harness/checkpoint.py:572). Thus tampering with `repo_base_commit`, `work_tree_oid`, `index_tree_oid`, or `tree_snapshot_digest` causes identity refusal.
>
> The accepted obs-017 live-run justification gap was not re-raised. Both reviewed ranges leave `harness_core` untouched, and I found no floor act. I could not execute the Python tests because `pytest` is unavailable in this environment; the verdict is based on the live diffs and surrounding source.
>
> VERDICT: `RATIFIED`

---

**Ratified at round 12, codex CLEAN (0 BLOCKING / 0 MAJOR / 0 MINOR / 0 NIT), VERDICT: RATIFIED.** The reviewgate receipt records this as the SHIP-class landing authorization (SHIP is the gate's only authorization token; RATIFIED is codex's verbatim clean-verdict word, reproduced above).
