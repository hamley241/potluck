# Three design questions before building part 3 (delivery) — opinion wanted, not a review

Context: the panel ruling at `conductor/docs/decisions/001-fix-round-delivery-boundary.md` moved the
delivery boundary to one delivery per ACCEPTED TASK. Part 1 (export) landed as potluck PR #19, part 2
(import) as PR #20 — both in `/Users/gp/development/autos/potluck`, gate 793, read the code there.

Part 3 builds **delivery**: an explicit operation that takes a result **selected by manifest digest**
and applies its cumulative patch to the operator's checkout **once**, running no doer, no gate and no
review. The ruling makes four safeguards REQUIRED: the expected repository identity **and** base; the
operator checkout still matching the **captured clean baseline**; preservation of the real index and
refs; all-or-nothing application, with a conflict **retaining** the result behind a **structured
refusal** — never a rebase, never an overwrite.

I want opinions on three points before I spend a build run, because each one is a contract I cannot
cheaply change later.

## Q1 — How does potluck check the "expected repository" when it cannot canonicalise?

The fleet's one canonicaliser is `conductor/harness/push_safety.canonical_repo` (https-only,
port-refusing, exactly `owner/repo`, lowercased). **potluck does not depend on conductor**, and at
your own suggestion part 1 records `repository.push_url_raw` — the raw `git remote get-url --push
origin` output, deliberately uncanonicalised — rather than copying a security-relevant parser into a
second repo.

So what should delivery actually compare?

- **(a)** Root-commit equality plus raw push-URL **string** equality, refusing on either mismatch, and
  declaring in the refusal and the docs that canonical destination comparison is the CALLER's job.
  Root commits are intrinsic to the history and cannot be spoofed by a remote URL; the URL string is
  recorded evidence, not a parsed identity.
- **(b)** Delivery takes an expected-identity ARGUMENT from the caller (the conductor, which can
  canonicalise) and compares it against what it observes — but potluck still cannot canonicalise what
  it observes, so this may just move the gap.
- **(c)** Move the canonicaliser into `harness_core` so both repos vendor ONE implementation. This is
  the only option that makes a canonical check possible inside potluck, and it is also the largest:
  harness_core is the fleet's shared core and a new export there is a contract of its own.

Which, and why? If (c), does it belong in this slice or as a prerequisite slice in `harness_core`?

## Q2 — What exactly is the "captured clean baseline", and where is it captured?

The manifest records `base_commit` and `output_tree_digest`, but **nothing about the operator's tree
at export time**. Part 1's export runs under the ordinary clean-tree precondition unless
`--allow-dirty` is passed, so "clean at `base_commit`" is *usually* true but is not recorded.

Candidates:

- **(a)** Delivery requires the operator checkout to be at `HEAD == manifest.base_commit` **and**
  clean — verifiable now by recomputing the operator tree's digest and comparing it with the digest of
  `base_commit^{tree}`, with no new manifest field.
- **(b)** The export manifest gains an explicit `operator_baseline_digest`, recorded at export time,
  and delivery compares against that. Stronger provenance, but it changes the part-1 manifest, which
  has already landed and been receipted.
- **(c)** Both: compute as in (a), and record as in (b) for the audit trail.

Note (a) is checkable with what exists today. Is that sufficient for the ruling's requirement, or does
"captured" specifically mean recorded at export time? Say which, because I will write the acceptance
criterion from your answer and an unreachable criterion is worse than a weaker one.

## Q3 — What does `--import-dir` without `--export-dir` deliver?

You asked part 3 to define this explicitly and to test that the complete candidate is delivered ONCE
through the manifest-selected boundary. Today that combination imports a seed and then DELIVERS the
cumulative patch directly from the run's worktree, which I wrongly called a defect and you correctly
said was not. Options:

- **(a)** Keep it: an importing run with no export delivers its complete candidate directly. Simple,
  already tested, but it is a SECOND delivery path that bypasses the manifest-digest boundary the
  ruling just built.
- **(b)** Refuse it once explicit delivery exists: a run either exports a result (to be delivered by
  digest) or does not import at all. One delivery path, at the cost of removing a working mode.
- **(c)** Keep it but route it through the same code: the run exports to a temporary location and
  delivers that by digest, so there is exactly one delivery implementation.

Which keeps "one delivery per accepted task" honest without inventing work?

Answer all three with reasons. Flag anything in my framing that is wrong. Do not write code.
