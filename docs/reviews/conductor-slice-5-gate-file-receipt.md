# conductor §5 — potluck `fix --gate-file` seam — code-review receipt

The machine-checkable block authorizes conductor Step-0 §5: the full
`git diff 675a3c6..7d085d0` — potluck's `fix --gate-file` seam (run the verification gate
from an external, immutable, identity-bound path outside the working tree), the §5 hop-2
gate-input mechanism. potluck-only; no harness_core/menu change, no vendoring.

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: 7d085d0f0ac77bc4799f8cd048666f48c49938a1
verdict: SHIP
date: 2026-07-26
findings: docs/reviews/conductor-slice-5-gate-file-code-review.md
```

**Verdict wording.** codex's verbatim round-3 verdict was **`RATIFIED`** (0 findings),
reproduced in the findings pointer. reviewgate's only authorization token is `SHIP`
(`_is_ship`), so the machine block records `verdict: SHIP`; RATIFIED is the reviewer's
literal clean-verdict word (RATIFIED→SHIP, standing decider ruling 2026-07-24).

**Provider independence (G4/G5).** codex (OpenAI, `gpt-5.6-sol`) ≠ doer (Anthropic).
SHIP-class.

**Range identity (obs-021 / obs-025).** `reviewed_head` names this commit's parent
(`7d085d0`, FULL 40-char OID — the short-hash refusal is logged in the fold notes). The
reviewed range is `675a3c6..7d085d0`, and origin/main sat at `675a3c6` at landing, so the
pushed range equals the reviewed range. Own receipt (obs-025).

**What §5 delivers.** `potluck fix --gate-file <path>` runs the gate from an EXTERNAL path
so the product tree stays clean (the clean-tree precondition holds) and the gate is never
an unattributed commit — the conductor passes the handoff `verify.sh` this way (hop 2).
The gate is made sound to the ruled threat: it must live outside the tree, its bytes are
FROZEN into the run-dir and the immutable copy is run + digested, and the digest is bound
into the tamper-evident `CheckpointIdentity` (`gate_binding_digest`, schema v2 — the
version discriminator). Resume resolves the gate from the frozen copy's PRESENCE (a file
fact), and `verify_identity` refuses any downgrade (frozen deleted → in-tree sentinel ≠
committed digest) or tamper (frozen edited → digest ≠ committed).

**Three-round convergence.** r1 (1 BLOCKING + 3 MAJOR: resume mode-binding / in-tree
refusal / immutable-execution / test faithfulness) → freeze + require-run-dir + in-tree
refusal; r2 (1 BLOCKING + 1 MAJOR: the run_inputs sidecar was editable + not identity-
bound) → moved the binding into the checkpoint identity (schema v2 discriminator), dropped
the gate fields from run_inputs, mode-from-file-presence; r3 RATIFIED. Full potluck suite
green (schema-2 identity; the identity-divergence test covers `gate_binding_digest`).

## Token spend (disclosed)

codex `exec` (gpt-5.6-sol), §5 CODE review, 3 rounds: r1 52,465 + r2 35,269 + r3 38,498 =
**126,232 tokens**. All build/fold work was $0 internal-dev (`authorized_act =
{deploys:false, publishes:false, spends:false}` → `internal_dev`).
