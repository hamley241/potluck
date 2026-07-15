# Rule: never bypass verification on commit

You MUST NOT use git flags that skip hooks or checks:
- no `git commit --no-verify`
- no `git push --no-verify`
- no `-n` shorthand for the above
- no disabling pre-commit / pre-push hooks

If a pre-commit hook is failing, fix the underlying issue. Bypassing the gate
defeats the entire purpose of the loop and hides real problems from the next
session and from review.
