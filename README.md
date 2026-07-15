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
  resolve.py             detect installed models, ask, write the resolved plan
  paths.py               data + user-config locations (clone vs. installed)
  runner.py              bounded execution + timeout escalation counter
  orchestrator.py        the loop + all escalation exits + prompt builders
  cli.py                 `potluck fix | resolve | doctor | setup`
  test_orchestrator.py   control-logic tests (run first, always)
  test_resolve.py        model-resolution tests
  test_runner.py         subprocess timeout/cleanup test
base/                    synced to every device
  rules/                 always-follow guardrails (anti-gaming)
  commands/              /bugfix, /harness-bugfix slash commands
  hooks/                 pre-tool-secret-scan.py — outbound tripwire (secrets+PII)
profiles/  personal/ phi/ ci/    per-environment overlays (config only)
pyproject.toml           packaging: `uv tool install`, ships base/ + profiles/
potluck                  CLI wrapper for the clone path (uv run)
test                     runs all suites via uv (no pytest/venv setup)
setup.sh                 clone-path installer (symlinks into ~/.claude + PATH)
verify.sh.example        per-PROJECT gate template (copy into each project repo)
```

`base/` and `profiles/` ship inside the wheel (as `harness/_data/`) for the `uv
tool install` path; a clone uses them in place. Machine-local state (`resolved.toml`)
always lives in `~/.config/potluck/`.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the only toolchain dependency. Everything
  runs through `uv`, which fetches Python 3.11 and pydantic for you.
  Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **[Claude Code](https://claude.com/claude-code) CLI (`claude`)** — **required**:
  it's the doer and the fallback for every role. Authenticate once by running
  `claude` and following the login prompt.
- **git** + a **Unix shell (bash)** — macOS or Linux; Windows via WSL.
- **Codex and/or Kimi CLIs** — *optional*. If on your `PATH` (or, on macOS, in
  their app-bundle locations) they're auto-detected and offered as the reviewer /
  tiebreaker; authenticate each (`codex login`, and Kimi's own auth). Without
  them, potluck falls back to Claude models — no setup required.

## Install

**Recommended — as a CLI tool (`uv`):**

```bash
uv tool install git+https://github.com/hamley241/potluck.git
potluck setup            # installs slash commands into ~/.claude, then asks which models to use
potluck doctor           # confirm which model backs each role
```

`potluck setup` copies potluck's slash commands, hooks, and guardrail rules into
`~/.claude` (merging file-by-file — it won't clobber your existing config) and
runs model resolution. Restart Claude Code afterward to pick up the new commands.
Override the target with `CLAUDE_HOME=… potluck setup`.

**Alternative — from a clone (to hack on it):**

```bash
git clone https://github.com/hamley241/potluck.git && cd potluck
./setup.sh personal      # symlinks base/ into ~/.claude + puts `potluck` on PATH via ~/.local/bin
./test                   # ALL SUITES PASS
```

Either way, **machine-local model choices live in `~/.config/potluck/`, never in
git** — re-run `potluck resolve` (or `potluck setup`) on each machine, and
re-authenticate each model CLI there.

## Quickstart

potluck runs inside **a project's git repo** and needs a gate script telling it
what "correct" means for that project:

```bash
cd your-project                       # must be a git repo
mkdir -p .claude
curl -LsSf https://raw.githubusercontent.com/hamley241/potluck/main/verify.sh.example -o .claude/verify.sh
chmod +x .claude/verify.sh
$EDITOR .claude/verify.sh             # keep only your stack's commands; exit 0 = pass, non-zero = fail

potluck fix \
  --spec "Fix the off-by-one in paginate()" \
  --acceptance "the pagination tests pass"
```

(Cloned instead? The template is already at `verify.sh.example` in the clone —
`cp verify.sh.example your-project/.claude/verify.sh`.)

## Examples

```bash
# Fix a bug, letting the gate define done:
potluck fix --spec "raise on negative quantity in Order.add_item" --acceptance "tests pass"

# Read the spec from a file (good for longer asks):
potluck fix --spec-file change.md --acceptance-file criteria.md

# Force the always-available Claude-only floor (no Codex/Kimi needed):
potluck resolve --claude-only && potluck fix --spec "…" --acceptance "…"

# See what's wired without changing anything:
potluck doctor
```

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
with a one-line reason; your changes and the debate log are left in place for you
to judge. A non-answer is never silently treated as "approved".

Inside Claude Code, the same loop is one command: `/harness-bugfix <describe the bug>`.

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
