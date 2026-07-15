# Rule: never modify the verification gate to make it pass

The verification gate (verify.sh, test config, lint config, typecheck config,
CI config) defines what "correct" means. It is the source of truth the loop
drives toward.

You MUST NOT:
- edit, weaken, or delete tests to make a failing suite pass
- modify linter/typechecker/formatter configs to silence errors instead of fixing them
- change verify.sh or any gate command to skip or short-circuit checks

If the gate is wrong (a test asserts the wrong thing, a rule is genuinely
misconfigured), STOP and surface it for human decision. Do not fix code by
moving the goalposts. A green gate you edited to be green is worthless.
