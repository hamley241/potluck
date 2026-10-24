# Acceptance: the lease

Graded against `the-lease-spec.md` and ruling 002 R3/R5. If a criterion is wrong or unreachable,
amend it here in writing and say why.

**A1.** `./test` exits 0 with `ALL SUITES PASS` and at least the 890 checks `main` has today.

**A2.** `HarnessConfig().escalation.retries_before_counting == 0`, and each of the three shipped
profiles resolves to 0.

**A3.** `harness_core`'s `EscalationConfig().retries_before_counting` is still 1; no file outside this
repository is modified (verified against every sibling repo's HEAD and status after the run).

**A4.** Under potluck's config, a bounded call whose factory always times out is invoked **exactly
once** and returns `timed_out`; under a bare core `EscalationConfig` it is invoked twice.

**A5.** `resolve_doer_timeout("doer:implement") == 1200`; `apply_fixes` and `respond` still 600.

**A6.** `c_max() == 2405` and `outer_min() == 72150` at defaults; `feature_seconds` defaults to 72150.

**A7.** Each shipped profile loads through `HarnessConfig.load` and passes `validate()`; the `ci`
profile has `feature_seconds == 33670` and `outer_min() <= 33670`.

**A8.** No comment beside a timeout or feature value still states 1205, 36150 or 16870 as current.

**A9.** `_effective_config_dict` includes `retries_before_counting`, and a resume whose checkpoint was
written under a different value is refused by the identity check, naming the divergence.

**A10.** A stubbed implement taking more than 600s of simulated time, under 1200, completes, and the
tree is not restored before it does.

**A11.** A `doer:implement` dispatch emits `prompt_size` with `step == "doer:implement"` and `bytes`
equal to the UTF-8 length of the prompt actually sent; above the threshold it also emits
`prompt_large`. The prompt text is byte-identical to before this slice.

**A12.** Nothing refuses, gates or changes behaviour on the prompt's size.

**A13.** Both `respond-timeout-band` slice documents carry an appended, dated supersession note naming
the new values; their original text is unchanged.

**A14.** `station-run.json` stays v1: field set unchanged.

**A15.** Every new test carries a comment stating what would have to break for it to fail.

**A16.** No `.<repo>.potluck-run-*` directory or `.git/worktrees` entry remains after the run.
