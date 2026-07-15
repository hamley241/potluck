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
whether to use it. v1 drives each model through its **local CLI**; if you pick
"API key" for a model whose CLI isn't installed, that's a documented seam that
isn't wired yet (see Seams) and resolution tells you so. Four outcomes, all valid:

```
all three available   →  doer=Claude   reviewer=Codex    tiebreaker=Kimi
Claude + Codex only    →  doer=Claude   reviewer=Codex    tiebreaker=Claude
Claude + Kimi only     →  doer=Claude   reviewer=Claude   tiebreaker=Kimi
Claude only            →  doer=Claude   reviewer=Claude   tiebreaker=Claude
```

**The Claude-only floor always works** — one `claude` CLI, three distinct models
across the roles: the doer on your session's default (e.g. Opus), the reviewer on
Sonnet, the tiebreaker on Haiku. These are passed as `--model` aliases to your
`claude` CLI; if your account can't reach one, that call escalates as "no signal"
(never a silent approval). But be honest about what it buys you: three Claude models share
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
3. **Human escalation on genuine deadlock** — after the cap, if the tiebreaker
   forms a 2-of-3 majority the issue resolves (per flow step 6); only a true
   split — no majority — is surfaced to you, never silently resolved in either
   model's favor.
4. **Timeout-with-escalation on hangs** — every external wait is time-boxed; one
   timeout retries transparently, repeated timeouts escalate (scattered slowness
   vs. the same step hanging repeatedly are distinguished). A timeout is **never**
   a verdict — a timed-out review is "no signal", not "approved".

All four are verified in `harness/test_orchestrator.py` (10 cases, no real model
calls). Model resolution is verified in `harness/test_resolve.py` (16 cases), and
subprocess cleanup in `harness/test_runner.py`.

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
  test_runner.py         subprocess timeout/cleanup test
base/                    synced to every device
  rules/                 always-follow guardrails (anti-gaming)
  commands/              /bugfix, /harness-bugfix slash commands
  hooks/                 pre-tool-secret-scan.py — outbound tripwire (secrets+PII)
profiles/  personal/ phi/ ci/    per-environment overlays (config only)
potluck                  CLI wrapper (symlinked onto PATH by setup.sh)
test                     runs all suites via uv (no pytest/venv setup)
setup.sh                 symlink base + profile into ~/.claude, then resolve
verify.sh.example        per-PROJECT gate template (copy into each project repo)
```

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the only toolchain dependency. `potluck`
  and `./test` run through `uv run`, which fetches Python 3.11 and pydantic for
  you, so you don't install those separately.
- **git** and a **Unix shell (bash)** — macOS or Linux; Windows needs WSL.
- **[Claude Code](https://claude.com/claude-code) CLI (`claude`)** — required: it
  is the doer and the fallback for every role. Authenticate it once.
- **Codex and/or Kimi CLIs** — *optional*. If on your `PATH` (or, on macOS, in
  their app-bundle default locations) they're auto-detected and offered as the
  reviewer/tiebreaker. Without them, potluck falls back to Claude models.
  Authenticate each one you enable.

## Getting started

```bash
git clone https://github.com/hamley241/potluck && cd potluck
./setup.sh personal            # read "What setup.sh touches" below first
potluck doctor                 # confirm which models back each role

# each project you run in needs a gate script:
cd your-project
cp /path/to/potluck/verify.sh.example .claude/verify.sh
$EDITOR .claude/verify.sh       # fill in your test/lint/build commands; exit 0 = pass

potluck fix --spec "Fix the pagination off-by-one" --acceptance "tests pass"
```

> **What `setup.sh` touches.** It symlinks `rules/`, `commands/`, and `hooks/`
> into `~/.claude`, **replacing** any existing symlinks of those names — back up
> your own Claude config first. It also symlinks the `potluck` CLI into
> `~/.local/bin` (make sure that's on your `PATH`). To try it without touching
> your real config: `CLAUDE_HOME=~/.potluck-home ./setup.sh personal`. The
> `potluck` CLI works straight from the clone without `setup.sh` — setup only
> adds it to `PATH` and installs the slash commands.

A successful run looks like:

```text
$ potluck fix --spec "add() subtracts instead of adding; fix to sum" --acceptance "tests pass"
potluck: profile=personal, debate=on, max_rounds=2
roles: reviewer=codex, tiebreaker=kimi
...
============================================================
  PASSED  |  Rounds: 0
============================================================
Debate log:
  [gate] {"label": "initial", "passed": true}
  [early_exit] {"reason": "no_blocking_or_major"}
```

An unresolved review or an unavailable model instead ends in `ESCALATED (...)`
with a one-line reason; the diff and debate log are left in place for you to
judge. A non-answer is never silently treated as "approved".

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
./test          # runs all three suites → ALL SUITES PASS
```

No pytest or venv setup — just `uv` on PATH. The suites are deterministic
harness contracts (no real model calls): `test_orchestrator` (control logic),
`test_resolve` (model resolution), `test_runner` (subprocess cleanup). Run this
on every new machine to confirm the core is intact before trusting a real run.

## Self-hosted

potluck builds potluck. The backend health check (`check_backend` /
`check_models` in `resolve.py`) and its four tests were written by potluck's own
`fix` loop, not by hand: Claude (doer) implemented against a spec, potluck's gate
(`./test`) settled correctness, and **Codex** independently reviewed the diff —
early-exit PASSED, 0 rounds, zero human edits to the code. The repo carries its
own `.claude/verify.sh`, so you can point `potluck fix` at potluck itself. See
commit [`ad514f0`](https://github.com/hamley241/potluck/commit/ad514f0).

## Seams (deliberate, honest)

- **API-key backends** — `resolve.py` wires the *local CLI* for Codex/Kimi. When
  a model is chosen as "API key" but no CLI is installed, that direct-API path is
  a documented seam, not yet wired — surfaced at resolve time rather than faked.
- **Large prompts via argv** — the Kimi backend passes its prompt as a command
  argument, so a very large diff can exceed the OS `ARG_MAX` limit. This surfaces
  as a clean `escalated_no_signal` (the tiebreak is skipped, never a crash or a
  false approval); Codex and Claude use stdin and aren't affected.
- **Divergent work** (architecture / research) is intentionally out of scope:
  potluck v1 writes code against a deterministic gate. Design review — where two
  models can agree a bad idea is good — needs a different shape and stays a human
  call.

## License

[MIT](LICENSE) © 2026 Goutham Patley. Use it, fork it, build on it.
