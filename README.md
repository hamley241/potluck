# potluck

A multi-model **code-writing** harness. Claude writes the code; a deterministic
gate settles correctness; an independent model reviews what was written; a
tiebreaker (or you) settles genuine deadlock. You bring whatever models you have
on hand — that's the potluck.

By **[hamley241](https://github.com/hamley241) (Goutham Patley)**. Derived from
the `hamley-soup` harness, narrowed to a single job: writing code.

`LLM proposes; framework disposes` — each layer catches a different class of
error: tools settle *correctness*, an independent model settles *judgment*, a
human settles genuine disagreement. Remove one and a failure mode comes back.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the only toolchain dependency; it fetches
  Python 3.11 and pydantic for you.
  Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Claude Code CLI (`claude`)** — **required** (the doer + the fallback for every
  role). Install per [claude.com/claude-code](https://claude.com/claude-code)
  (e.g. `npm i -g @anthropic-ai/claude-code`), then run `claude` once to log in.
- **git** + a **Unix shell (bash)** — macOS or Linux; Windows via WSL.
- **Codex and/or Kimi CLIs** — *optional* upgrades for the reviewer/tiebreaker
  roles. Install and authenticate each per its own docs (once installed, they're
  auto-detected on `PATH` or in their macOS app-bundle locations). Skip them and
  potluck falls back to Claude models — nothing to configure.

## Install

**Recommended — as a CLI tool (`uv`):**

```bash
uv tool install git+https://github.com/hamley241/potluck.git
potluck setup            # installs slash commands into ~/.claude, then asks which models to use
potluck doctor           # confirm which model backs each role
```

`potluck setup` copies potluck's slash commands, hooks, and guardrail rules into
`~/.claude`, merging **file-by-file** (it won't replace your existing config),
then runs model resolution. Restart Claude Code afterward to pick up the new
commands. Override the target dir with `CLAUDE_HOME=… potluck setup`. To review
or remove what it installed, look in `~/.claude/{commands,hooks,rules}` (potluck's
files: `potluck.md`, `potluck-bugfix.md`, `pre-tool-secret-scan.py`, `no-*.md`);
re-run `potluck setup` to refresh them.

**Alternative — from a clone (to hack on it):**

```bash
git clone https://github.com/hamley241/potluck.git && cd potluck
./setup.sh personal      # symlinks base/ into ~/.claude + puts `potluck` on PATH via ~/.local/bin
./test                   # ALL SUITES PASS
```

Machine-local model choices live in `~/.config/potluck/`, never in git — re-run
`potluck resolve` (or `potluck setup`) and re-authenticate each model CLI on every
machine.

## Quickstart

potluck runs **inside a project's git repo** and needs a gate script that defines
"correct" for that project. Here's a complete one for a Python/pytest project:

```bash
cd your-project                       # must be a git repo
mkdir -p .claude
cat > .claude/verify.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
pytest -q                             # replace with your project's real checks
EOF
chmod +x .claude/verify.sh

potluck fix \
  --spec "Fix the off-by-one in paginate()" \
  --acceptance "the pagination tests pass"
```

The gate is just a shell script: **exit 0 = pass, non-zero = fail**. Put whatever
proves correctness for your stack in it (tests, typecheck, lint, build). A richer
multi-stack template is at `verify.sh.example` in the repo — copy it and delete
the lines you don't use.

**Exit-code convention.** The gate distinguishes *code being wrong* from *the gate
being unable to run*: `0` = green (correctness settled), `1` = red (a check ran and
reported failure), `2` = **no signal** (the gate could not execute — a missing tool,
venv, or interpreter), and any other non-zero code is conservatively treated as red.
Exit `2` routes to `ESCALATED_NO_SIGNAL` rather than a red gate, so a broken
environment (`uv: command not found`) isn't misread as broken code and every
debugging effort pointed at the doer. Fail closed with `exit 2` when a required tool
is absent — see `verify.sh.example` for the guard.

potluck uses the **`personal`** profile by default; select another with
`--profile ci` (see [Profiles](#profiles)).

## Examples

```bash
# Fix a bug, letting the gate define done:
potluck fix --spec "raise on negative quantity in Order.add_item" --acceptance "tests pass"

# Longer ask: read spec/criteria from plain text or markdown files:
potluck fix --spec-file change.md --acceptance-file criteria.md

# Force the Claude-only path (no Codex/Kimi needed):
potluck resolve --claude-only && potluck fix --spec "…" --acceptance "…"

# See what's wired, change nothing:
potluck doctor
```

`potluck doctor` prints what it found and what it would use:

```text
$ potluck doctor
potluck doctor -- detected backends:
  claude   /Users/you/.local/bin/claude
  codex    /Applications/ChatGPT.app/Contents/Resources/codex
  kimi     (not found)
Would resolve (auto) to:  doer=claude  reviewer=codex  tiebreaker=claude
```

A successful `fix` looks like:

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

**Inside Claude Code**, the same loop is one command (after `potluck setup` and a
session restart): `/potluck <describe the bug>`. It shells out to
`potluck fix` in the current repo — which needs the same `.claude/verify.sh` gate
— and summarizes the outcome back to you.

## How it works

potluck fills three roles. The doer is always Claude Code. The reviewer and
tiebreaker are resolved from **what you have installed and chose to use**:

| Role       | Preferred | Falls back to  |
|------------|-----------|----------------|
| doer       | Claude    | (always Claude)|
| reviewer   | Codex     | a Claude model |
| tiebreaker | Kimi      | a Claude model |

`potluck resolve` detects what's on the machine and asks, per external model,
whether to use it. Four outcomes, all valid:

```
all three available   →  doer=Claude   reviewer=Codex    tiebreaker=Kimi
Claude + Codex only    →  doer=Claude   reviewer=Codex    tiebreaker=Claude
Claude + Kimi only     →  doer=Claude   reviewer=Claude   tiebreaker=Kimi
Claude only            →  doer=Claude   reviewer=Claude   tiebreaker=Claude
```

**Claude-only works with just the `claude` CLI** — three distinct models across
the roles (doer on your session default, e.g. Opus; reviewer on Sonnet;
tiebreaker on Haiku). If your account can't reach a model alias, that call
escalates as "no signal" rather than a silent approval. Be honest about what it
buys you, though: same-lineage models make **correlated** mistakes. A
fresh-context Claude reviewer still catches plenty (it never saw the doer write
the code), but adding Codex/Kimi is a real upgrade in *independence* — foreign
models fail differently, so they catch Claude's blind spots. Claude-only is the
floor; a foreign reviewer is the ceiling.

The loop:

```
1. Doer implements against spec + acceptance criteria          [Claude Code]
2. GATE: verify.sh (test/typecheck/lint/build) must pass       ← correctness, by tools
3. Reviewer reviews the diff AGAINST spec → structured verdict [Codex or Claude]
   └─ nothing blocking/major → done (early exit)
4. Doer responds per issue: accept(+plan) | reject(+reasoning) ← can push back
5. Reviewer responds to rejections only → concede or hold
   └─ loop to 4 while unresolved blocking remain AND rounds left (cap = 2)
6. Deadlock after cap → tiebreaker adjudicates the disputed issue (blind to which
   side is which): sides with doer → clears; sides with reviewer or unsure → escalates to you
7. Doer applies agreed fixes
8. GATE again → must pass
9. PASSED → diff + debate log surfaced for your approval
```

**Four control guarantees:**

1. **Round cap with early exit** — debate is capped (default 2) and exits early
   when the review is clean. The cap is a ceiling, not a target.
2. **Structured verdicts** — every cross-model exchange is typed JSON
   (`schemas.py`), so deadlock is *detected*, not guessed.
3. **Human escalation on genuine deadlock** — after the cap, the tiebreaker
   adjudicates each still-blocking issue; anything it can't resolve in the doer's
   favor is surfaced to you, never silently decided for a model.
4. **Timeout-with-escalation on hangs** — every external wait is time-boxed; one
   timeout retries transparently, repeated timeouts escalate (scattered slowness
   vs. the same step hanging repeatedly are distinguished). A timeout is **never**
   a verdict — a timed-out review is "no signal", not "approved".

All four are verified in `harness/test_orchestrator.py` (10 cases, no real model
calls); model resolution in `harness/test_resolve.py` (16 cases); subprocess
cleanup in `harness/test_runner.py`.

## Profiles

- `personal` — full setup, all resolved models, interactive (the default).
- `phi` — debate stays on (code is PII-free); the tripwire hook guards accidental
  leaks; set `human_only_paths` to your security/PHI modules (confirm with security).
- `ci` — non-interactive, deterministic-gate-only (no human to escalate to, and
  model steps are the likeliest to hang); resolution runs `--auto`.

Select one per run with `--profile <name>`.

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
  test_*.py              deterministic control-logic + resolution + runner tests
base/                    installed into ~/.claude by setup
  rules/                 always-follow guardrails (anti-gaming)
  commands/              /potluck, /potluck-bugfix slash commands
  hooks/                 pre-tool-secret-scan.py — outbound tripwire (secrets+PII)
profiles/  personal/ phi/ ci/    per-environment overlays (config only)
pyproject.toml           packaging: `uv tool install`, ships base/ + profiles/
potluck                  CLI wrapper for the clone path (uv run)
test                     runs all suites via uv (no pytest/venv setup)
setup.sh                 clone-path installer (symlinks into ~/.claude + PATH)
verify.sh.example        per-PROJECT gate template
```

`base/` and `profiles/` ship inside the wheel (as `harness/_data/`) for the `uv
tool install` path; a clone uses them in place. Machine-local state
(`resolved.toml`) always lives in `~/.config/potluck/`.

## Run the tests

```bash
./test          # runs all three suites → ALL SUITES PASS
```

No pytest or venv setup — just `uv` on PATH. The suites are deterministic harness
contracts (no real model calls). Run this on every new machine to confirm the
core is intact before trusting a real run.

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
  argument, so a very large diff can exceed the OS `ARG_MAX` limit. The diff is
  now **packed to fit**: the fixed framing (instructions, spec, criteria) is kept
  whole and the diff is trimmed at *file boundaries* — whole files only, never a
  partial hunk — to a byte budget derived from `sysconf(SC_ARG_MAX)` minus the
  environment and a safety margin. Every dropped file leaves a visible
  `# omitted from this review (prompt budget): …` marker so absence is never
  silent, and a `prompt_packed` event lists the complete set of omissions. The
  seam is now the **residual**: if the framing *alone* exceeds `ARG_MAX` there is
  nothing left to trim, so the call still surfaces as a clean
  `escalated_no_signal` (the tiebreak is skipped, never a crash or a false
  approval). Codex and Claude use stdin and are never packed — a pipe has no
  `ARG_MAX` to respect, and (per `docs/observations.md` 001) their large-prompt
  drift is *measured* via `prompt_size` / `prompt_large` events, never
  "fixed" by dropping files.
- **Divergent work** (architecture / research) is intentionally out of scope:
  potluck v1 writes code against a deterministic gate. Design review — where two
  models can agree a bad idea is good — needs a different shape and stays a human
  call.

## License

[MIT](LICENSE) © 2026 Goutham Patley. Use it, fork it, build on it.
