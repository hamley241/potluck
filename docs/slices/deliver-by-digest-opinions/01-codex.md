I’ll read the ruling and the export/import code, then give a recommendation for each contract. I won’t change any files.

My recommendations are **Q1: (a), Q2: (a) with a precise definition, Q3: (b)**.

**Q1 — Compare recorded evidence exactly; leave canonical destination policy with the caller.**

Use the root-commit set and exact `push_url_raw` equality, alongside the separately required base check. Fail closed when required evidence is missing or unreadable; two missing URLs must not count as verified identity.

The expectation must come from the caller’s selected task/result binding, rather than simply treating whatever manifest happens to be at a path as authoritative. Selecting the manifest by an independently supplied digest provides that binding.

This is a conservative repository check: equivalent URL spellings can produce refusals. That is acceptable. Explain that limitation in the contract, and report expected and observed facts in refusals.

Two qualifications to your framing:

- Root commits identify shared history, **not a unique repository**. Forks share them. They complement the URL check; they do not authenticate its destination.
- A configured push URL is evidence about configuration, not proof of ownership or authorization. Canonicalisation would not provide that proof either.

Option (b), by itself, cannot close the gap you identify. I would not make (c) a prerequisite for this delivery slice. If the fleet later requires potluck itself to enforce canonical destination policy, extract the canonicaliser in a separate `harness_core` prerequisite slice, with an explicit consumer migration. The ruling currently says no core change is necessary.

**Q2 — The recorded base commit can capture the clean baseline without another manifest field.**

Choose (a), and define it explicitly:

> The delivery baseline is the clean checkout of the manifest’s recorded `base_commit`. Delivery requires operator HEAD to equal that commit, the real index to represent that commit’s tree, and the operator working tree, including non-ignored untracked files, to match that tree.

That is sufficient for the ruling’s equality safeguard. The commit already durably identifies the expected clean tree. “Captured” need not mean a separately stored digest of the same tree.

But **your proposed tree-digest comparison alone does not establish cleanliness**. A digest computed through a temporary index can match the base while the real index contains staged changes that the working tree reverses. Check the real index independently, without refreshing or rewriting it.

An export made with `--allow-dirty` does not weaken this delivery requirement. It remains deliverable only onto the clean original base. The conductor should still capture the initial operator state for its audit obligations; that is distinct from adding a redundant export field.

I would write the acceptance criterion around those observable equalities, not “unchanged since export.” The latter is historical continuity that the existing manifest cannot establish. Also refuse any ignored-path obstruction that application would overwrite; ignored files being outside the digest boundary does not authorize clobbering them.

**Q3 — Refuse import without export.**

Choose (b), with an early, actionable refusal directing the caller to export and then explicitly deliver the accepted manifest digest.

The current mode is working as implemented: `cmd_fix` calls `run.deliver()` when no export directory is supplied, and `run.change()` computes the complete cumulative patch. Calling that an existing implementation defect would still be wrong.

Nevertheless, preserving it would undermine the new boundary. Option (c) unifies implementation, but automatically exporting and delivering after a station passes still does not establish that the cumulative result was independently accepted. **One delivery implementation is not the same contract as one delivery per accepted task.**

This does remove a recently added mode. State that explicitly in the slice while preserving ordinary, non-importing CLI delivery, which the ruling expressly protects. A two-round acceptance test should export both rounds, explicitly select round 1’s digest, and deliver its complete candidate once.

Finally, scope “once” carefully: potluck can enforce an explicit, digest-bound application and refuse subsequent baseline mismatch. **Durable exactly-once acceptance across retries, crashes, empty patches, or different results for the same task requires caller orchestration or a delivery receipt contract.** Do not accidentally promise that broader guarantee merely because there is one delivery command.