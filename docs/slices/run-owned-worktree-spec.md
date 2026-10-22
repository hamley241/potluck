# Give each run its own worktree, so the refs it moves are ones it owns

A panel ruled, unanimously, that potluck must **never move a ref the run does not exclusively
control**. Today it moves the user's branch: it absorbs a doer commit with `reset --soft <base>`
and, on abandonment, resets hard to the base. Both move a ref shared with anything else writing
that checkout, and a reproduced case destroys a foreign commit and its file outright. Guarding it
failed twice — ancestry does not prove ownership, and a foreign commit landing inside the doer
window is indistinguishable from the doer's own in a shared checkout under one user and one git
identity.

The answer is not a better guard. It is to make the run's refs run-owned.

## What to build

At run start, create a worktree detached at the run's base commit and run the doer and the gate
there. A detached worktree HEAD is a ref the run exclusively controls, so absorbing a doer commit
and rewinding become sound again — and the user's branch is never touched at all.

Decide and write down, because the spec deliberately does not fix it:

* **Where the run's output goes on SUCCESS.** potluck's contract today is that the work is left as
  UNCOMMITTED change in the operator's tree. A worktree breaks that by construction, since the
  work is somewhere else. The strong candidate is to apply the resulting change to the operator's
  working tree at the end — which moves no ref and preserves the existing contract — but weigh it
  against simply reporting the worktree path. Whatever you choose, say why in the code.
* **What happens on FAILURE.** The attraction of this design is that there is nothing to clean up
  in the operator's tree, because nothing was ever written there. Make that true rather than
  nearly true.
* **Whether the operator's tree must still be clean to start.** The precondition exists so a
  restore has a known baseline. If the run no longer writes there, re-examine it rather than
  carrying it forward unexamined — and if you keep it, say what it is still for.

## The trap that will break the gate if you miss it

The fleet's repos resolve their sibling by RELATIVE PATH. `menu/pyproject.toml` declares
`harness-core = { path = "../harness_core", editable = true }`, and potluck's and conductor's
`./test` default `CORE="${CORE_REPO:-$REPO/../harness_core}"`. A worktree placed anywhere that is
not a sibling of `harness_core` resolves `../harness_core` to nothing, and the gate fails as a
TOOLCHAIN error — exit 2, no signal — which will look like the run's fault and is not.

Handle it deliberately: place the worktree so the sibling resolves, or pass the core path
explicitly, or detect and refuse with a message naming this cause. Do not let it surface as a
mysterious no-signal gate.

## Hard constraints

1. **The operator's branch ref is never moved.** Not by absorb, not by restore, not on any path.
   This is the ruling; everything else is detail.
2. **No commit is ever destroyed that the run did not create in its own worktree.**
3. The worktree is cleaned up when the run ends, including on failure — no orphaned worktrees and
   no stale `.git/worktrees` entries accumulating over runs.
4. Ignored files stay preserved on every restore path; no `-x` is reintroduced anywhere.
5. Existing refusals and preconditions keep firing unless you have explicitly re-examined one and
   recorded why it changed.
6. `.claude/verify.sh` stays green.

## The durable half

The test that matters is the one that reproduces the destruction this exists to prevent: a
FOREIGN session commits to the operator's branch while a run is in flight, the run then abandons,
and afterwards the foreign commit is still reachable, its file still present, and the operator's
branch ref, HEAD and `git status` are exactly as the foreign session left them.

Also pin that no worktree survives a run, successful or failed.
