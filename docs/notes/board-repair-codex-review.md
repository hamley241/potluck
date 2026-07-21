# Board-repair — codex adversarial review record

**This review spanned the two-repo board-repair diff (menu + potluck).** This
copy lives in `potluck`; an identical record lives in `menu`
(`docs/notes/board-repair-codex-review.md`). Receipts live in the repo they
justify — each repo's commit receipt line points at its own in-tree copy of this
file, never across repos, so every clone carries its own evidence.

potluck's change in this board-repair is the `./test` wrapper patch (fail-closed
on missing `uv`; packaging check uses a uv-provided 3.11 interpreter, never a
bare `python3`). codex's read-only sandbox ran in the **menu** repo, so the
potluck change was reviewed from the diff text; pass 1 finding 3 records codex's
verdict on it ("The supplied potluck wrapper is clean…"), and pass 2 delta A
re-confirmed the fail-closed behaviour.

**Reviewer:** codex (OpenAI), model `gpt-5.6-sol` — independent, cross-provider,
fresh context, read-only sandbox in the menu repo.
**Reviewed range (board-repair changeset vs origin):**
menu `031a193..HEAD` · potluck `375f248..HEAD` (this commit).
**Invoked by:** the builder, non-interactively, per the decider's standing
process ruling (one manual codex review before push).

Two passes were run. Pass 1 HELD on two MAJOR findings; the decider dispositioned
both; pass 2 (scoped to the deltas) returned SHIP with one NIT, which was fixed
by applying the decider's existing guardrail (see the closing note).

---

## Pass 1 — full diff (session `019f82b1-c256-76d3-8b6a-27e9c3afd082`) — VERDICT: HOLD

1. **CLEAN — Review point 1 — `harness/test_draft_from.py:574-600`.** The original slice-3e test indisputably asserted receipt content, including parsed counts, citations, named divergence, and guarantee tiers. Git history confirms commit `d5802e9` introduced the test while containing `killed-slice-3e-receipt-EVIDENCE.md` but not the asserted `slice-3e-step0-reconciliation.md` path. The patch removes both the runner call and test implementation rather than repointing it, which correctly applies the content-versus-existence caveat.

2. **CLEAN — Review point 2 — `harness/test_handoff.py:429-464`; `docs/notes/handoff-golden-DECLARED-GAP.md:1-66`.** The fixture provenance is confirmed by the original suite header, its hard-coded `runs/engine-extraction-003` path, and `.gitignore:23`; no hand-shaped JSON or replacement golden is introduced. Retirement is loud: `_run_all()` unconditionally prints a prominent declared-gap banner and returns 0, with no skip-if-absent branch. Both restoration conditions preserve an independent, human-verified oracle, and the note correctly requires rewiring the dormant tests to the tracked fixture path upon restoration.

3. **MAJOR — Review point 3 — `.claude/verify.sh:26-39`.** Menu does not satisfy the stated requirement that it fail closed whenever `uv` is absent: `command -v uv` is checked only when `.venv/bin/python` is missing. If an existing executable `.venv/bin/python` remains on the machine while `uv` is unavailable, the wrapper proceeds and runs the full gate, contrary to Ruling 3's unconditional "both must fail closed on missing uv." Move the `uv` availability check outside the venv-bootstrap conditional, or narrow the ruling if reuse of an existing environment is intentionally allowed. The supplied potluck wrapper is clean: it checks `uv` unconditionally, exits 2 with installation guidance, and its patched path uses `uv run` rather than bare `python3`.

4. **CLEAN — Review point 4 — menu `harness/test_*.py` suite-wide path sweep.** No third test dependency on gitignored or untracked repository state was found. The other repository-document readers target tracked slice-3b, slice-3c, slice-3d, and blind-12b receipts; fixture readers target tracked `harness/fixtures` content; generated run files are created beneath temporary directories. The only remaining literal `runs/` dependency is the deliberately dormant `test_handoff` code, while `test_diverge` mentions it only in prose.

5. **MAJOR — fallback — `harness/test_handoff.py:431-464`; `harness/handoff.py` public extractor implementation.** Retirement leaves the entire handoff extractor with zero executing test coverage; no other `harness/test_*.py` references `extract_handoff`, `HandoffPlan`, `DeclaredGap`, or `_verify_verbatim`. Consequently, zero-covered behavior includes fixture round-trip fidelity, preservation of reopening conditions and verbatim rulings, priced-risk resolution and trigger carriage, rejection of acceptance items lacking a check or declared gap, and the rule that declared gaps cap state at `READY_FOR_DECISION` while gap-free bundles reach `CLEAN`. The declared-gap note truthfully acknowledges this under-protection, but it remains a material regression until an independent golden is restored.

**Overall verdict: HOLD**

### Decider dispositions of pass 1
- Points 1, 2, 4 — accepted as CLEAN.
- Point 3 — defect confirmed; the `uv` check hoisted to unconditional (ruling NOT narrowed). This is the potluck-relevant point: potluck was already clean; menu was brought to match it.
- Point 5 — upheld; disposition changed to add INTERIM, bounded contract coverage in menu (`harness/test_handoff_interim.py`); meta-observation 017 banked.

---

## Pass 2 — scoped deltas (session `019f82c8-4a98-7e63-a4f5-0f24c5b51140`) — VERDICT: SHIP

1. **NIT — Delta B — `harness/test_handoff_interim.py:115-119`:** The `_priced_ledger_entry` docstring explicitly names and describes private implementation function `_resolve_priced_risks`, contradicting the suite header's claim at lines 27–28 that no private function is "called, stubbed, or named." This does not contaminate the test assertions or inputs—the actual setup uses public schemas and follows the documented contract that risk IDs resolve through ledger concerns—but it is implementation-coupled commentary that could become stale after a correct refactor. This is against the decider's guardrail literally, though not materially enough to hold.

2. **Delta A is clean — `.claude/verify.sh:24-32`:** The `command -v uv` guard executes unconditionally before `.venv` inspection. With an existing executable `.venv/bin/python` and a PATH lacking `uv`, I observed the script print installation instructions and "Exiting 2 (no signal)," then exit 2. The only bootstrap command is `uv sync`; no path invokes bare `python3` or pip. Its failure is also converted to exit 2 with an actionable message.

3. **Delta B is otherwise clean — `harness/test_handoff_interim.py:1-313` and `docs/notes/handoff-golden-DECLARED-GAP.md:45-64`:** The tests remain within the five authorized behaviors, construct inputs through public schemas/dataclasses, call only `extract_handoff`, and assert observable emitted content, public bundle provenance/state, or documented exceptions naming the offending ID. The assertions follow the extractor docstring, public dataclass contracts, MENU-WIRING §8, and §4c rather than renderer formatting or ordering. Both required INTERIM labels accurately distinguish tier-TESTED extractor logic from tier-PROMISED acceptance fidelity.

**SHIP**

---

## Closing note — the NIT

The pass-2 NIT was fixed before commit by rephrasing the `_priced_ledger_entry`
docstring to cite the extractor's documented failure condition (3) instead of
naming a private function. Per decider ruling (2026-07-20), mechanically applying
an already-ruled guardrail to a documentation line is EXECUTION of an existing
ruling, not a new decision, and does not itself trigger re-review. The interim
suite was re-run green (14/14) after the fix.
