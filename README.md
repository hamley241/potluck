# potluck

A multi-model **code-writing** harness. Claude writes the code; a deterministic
gate settles correctness; an independent model reviews what was written; a
tiebreaker (or you) settles genuine deadlock. You bring whatever models you have
on hand — that's the potluck.

By **[hamley241](https://github.com/hamley241) (Goutham Patley)**. Derived from
the `hamley-soup` harness, narrowed to a single job: writing code.

## The idea in one line

`LLM proposes; framework disposes` — Claude implements, a deterministic gate
settles *correctness*, an independent model settles *judgment*, and a human
settles genuine disagreement. Each layer catches a different class of error;
remove one and a failure mode comes back.

## Bring your own models

potluck fills three roles. The doer is always Claude Code. The reviewer and
tiebreaker are resolved from **what you have installed and want to use**:

| Role       | Preferred | Falls back to        |
|------------|-----------|----------------------|
| doer       | Claude    | (always Claude)      |
| reviewer   | Codex     | a Claude model       |
| tiebreaker | Kimi      | a Claude model       |

`potluck resolve` detects what's on the machine and asks, per external model,
whether to use it (local CLI or API key). Four outcomes, all valid:

```
all three available   →  doer=Claude   reviewer=Codex    tiebreaker=Kimi
Claude + Codex only    →  doer=Claude   reviewer=Codex    tiebreaker=Claude
Claude + Kimi only     →  doer=Claude   reviewer=Claude   tiebreaker=Kimi
Claude only            →  doer=Claude   reviewer=Claude   tiebreaker=Claude
```

**The Claude-only floor always works** — one `claude` CLI, three distinct models
across the roles. But be honest about what it buys you: three Claude models share
a lineage, so their mistakes are **correlated**. A fresh-context Claude reviewer
still catches plenty (it never saw the doer write the code). Adding Codex/Kimi is
a real upgrade in *independence* — foreign models fail differently, so they catch
Claude's blind spots — not just redundancy. Claude-only is the floor; a foreign
reviewer is the ceiling.

## Flow

```
1. Doer implements against spec + acceptance criteria          [Claude Code]
2. GATE: verify.sh (test/typecheck/lint/build) must pass       ← correctness, by tools
3. Reviewer reviews the diff AGAINST spec → structured verdict [Codex or Claude]
   └─ nothing blocking/major → done (early exit)
4. Doer responds per issue: accept(+plan) | reject(+reasoning) ← can push back
5. Reviewer responds to rejections only → concede or hold
   └─ loop to 4 while unresolved blocking remain AND rounds left (cap = 2)
6. Deadlock after cap → tiebreaker adjudicates (2-of-3 proceeds; split → human)
7. Doer applies agreed fixes
8. GATE again → must pass
9. PASSED → diff + debate log surfaced for your approval
```

## The four control guarantees

1. **Round cap with early exit** — debate is capped (default 2) and exits early
   when the review is clean. The cap is a ceiling, not a target.
2. **Structured verdicts** — every cross-model exchange is typed JSON
   (`schemas.py`), so deadlock is *detected*, not guessed.
3. **Human escalation on disagreement** — unresolved blocking issues after the
   cap are surfaced to you, never silently resolved in either model's favor.
4. **Timeout-with-escalation on hangs** — every external wait is time-boxed; one
   timeout retries transparently, repeated timeouts escalate (scattered slowness
   vs. the same step hanging repeatedly are distinguished). A timeout is **never**
   a verdict — a timed-out review is "no signal", not "approved".

All four are verified in `harness/test_orchestrator.py` (8 cases, no real model
calls). Model resolution is verified in `harness/test_resolve.py` (8 cases).

## Layout

```
harness/                 the invariant core (Python)
  schemas.py             typed verdicts — the contracts the loop branches on
  config.py              thresholds + resolved model backends (role → backend)
  resolve.py             detect installed models, ask, write .resolved.toml
  runner.py              bounded execution + timeout escalation counter
  orchestrator.py        the loop + all escalation exits + prompt builders
  cli.py                 `potluck fix | resolve | doctor`
  test_orchestrator.py   control-logic tests (run first, always)
  test_resolve.py        model-resolution tests
base/                    synced to every device
  rules/                 always-follow guardrails (anti-gaming)
  commands/              /bugfix, /harness-bugfix slash commands
  hooks/                 pre-tool-secret-scan.py — outbound tripwire (secrets+PII)
profiles/  personal/ phi/ ci/    per-environment overlays (config only)
potluck                  CLI wrapper (symlinked onto PATH by setup.sh)
setup.sh                 symlink base + profile into ~/.claude, then resolve
verify.sh.example        per-PROJECT gate template (copy into each project repo)
```

## Getting started

```bash
git clone <repo> && cd potluck
./setup.sh personal            # symlinks + interactive model resolution
potluck doctor                 # confirm which models back each role

# run the loop inside any project that has .claude/verify.sh
cd your-project
potluck fix --spec "Fix the pagination off-by-one" --acceptance "tests pass"
```

Re-run `potluck resolve` on each machine (`--auto` uses everything detected,
`--claude-only` forces the floor). **Credentials and machine-local model paths
never sync** — `.gitignore` blocks `.resolved.toml` and any credential files, so
re-authenticate each model CLI per machine.

## Profiles

- `personal` — full setup, all resolved models, interactive.
- `phi` — debate stays on (code is PII-free); the tripwire hook guards accidental
  leaks; set `human_only_paths` to your security/PHI modules (confirm with security).
- `ci` — non-interactive, deterministic-gate-only (no human to escalate to, and
  model steps are the likeliest to hang); resolution runs `--auto`.

## Run the tests

```bash
python3.11 -m harness.test_orchestrator   # control logic  → ALL PASS
python3.11 -m harness.test_resolve        # model resolution → ALL PASS
```

Do this on every new machine after `setup.sh` to confirm the core is intact
before trusting a real run.

## Seams (deliberate, honest)

- **API-key backends** — `resolve.py` wires the *local CLI* for Codex/Kimi. When
  a model is chosen as "API key" but no CLI is installed, that direct-API path is
  a documented seam, not yet wired — surfaced at resolve time rather than faked.
- **Divergent work** (architecture / research) is intentionally out of scope:
  potluck v1 writes code against a deterministic gate. Design review — where two
  models can agree a bad idea is good — needs a different shape and stays a human
  call.
