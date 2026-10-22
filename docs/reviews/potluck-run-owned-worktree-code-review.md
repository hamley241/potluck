# Run-owned worktree — codex findings, rounds 1–10

| r | id | sev | outcome |
|---|---|---|---|
| 1 | `run-worktree-cleanup-crash-window` | BLOCKING | confirmed by entering the window; folded |
| 2 | `run-worktree-add-failure-orphan` | BLOCKING | **my r1 fold reintroduced the orphan**; folded |
| 3 | `run-worktree-mkdtemp-claim-window` | BLOCKING | part-accepted (claim moved to creation), part-REJECTED with evidence: the test written for it passed with the change reverted, and was deleted |
| 4 | `run-worktree-cleanup-not-guaranteed` | BLOCKING | accepted; indicted **A5 itself** as unachievable |
| 5 | `run-worktree-sweep-liveness` | BLOCKING | sweep REMOVED rather than fixed — it raced a concurrent run both ways |
| 6 | three findings | BLOCKING ×2, MINOR | admin entry unverified; A5 formally amended; a warning promising a sweep I had deleted |
| 7 | `run-worktree-cleanup-observable-errors` | BLOCKING | unverifiable treated as absent; report bypassable |
| 8 | `run-worktree-keyboardinterrupt-report` | BLOCKING | `except Exception` misses Ctrl-C |
| 9 | `run-worktree-survivor-report-can-raise` | MAJOR | last-resort report added |
| 10 | — | — | **SHIP, zero findings** |

## The shape worth keeping

The first commit delivered the property: the operator's branch is never moved, and the
reproduced commit destruction is impossible. It was green throughout. **Every finding after that
concerned orphaned directories, which cost no correctness at all** — and three were defects
introduced by the previous fold.

The severity curve is the signal: six BLOCKING, then MAJOR, then clean. Round 5 was the one that
should have prompted the scope question, and it took until round 5's finding to ask it.
