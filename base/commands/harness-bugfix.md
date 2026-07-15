# /harness-bugfix — run the full multi-model loop

Run the potluck orchestrator on the current project. This launches the full
loop: Claude (doer) → gate → reviewer → debate → tiebreaker (if needed) →
human escalation if unresolved. The reviewer and tiebreaker are whatever
`potluck resolve` wired on this machine — Codex/Kimi if installed, otherwise
distinct Claude models.

**Prerequisites:** the current project must have `.claude/verify.sh` set up,
and `potluck resolve` must have been run once on this machine.

Do the following steps in order:

1. Check that `.claude/verify.sh` exists. If not, tell the user to copy
   `verify.sh.example` from the harness repo and fill in their project's
   test/lint/build commands. Stop here if missing.

2. Run potluck using `potluck` (which is on PATH after setup.sh):

```bash
potluck fix --spec "$ARGUMENTS" --log-file "$(mktemp /tmp/potluck-debate-XXXXXX.json)" 2>&1
```

   If potluck is not found, fall back to the full path:
```bash
"$HOME/.local/bin/potluck" fix --spec "$ARGUMENTS" 2>&1
```

3. After the run completes, summarize:
   - Outcome (passed / escalated)
   - How many debate rounds
   - Any issues raised and how they were resolved
   - If escalated: what the human needs to decide

4. If the outcome is "passed", show the diff with `git diff` and ask
   the user if they want to commit.

5. If escalated, show the unresolved issues and ask the user what to do.
