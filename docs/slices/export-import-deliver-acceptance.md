# Acceptance: the station EXPORTS — part 1 of 3

Graded against `export-import-deliver-spec.md` and the panel ruling it cites. Every criterion was
checked for **reachability on this checkout** before being written. **If one turns out to be
unreachable or wrong, amend it in writing here and say why** — an unmet absolute left standing makes
the record say the work failed its own acceptance.

## The gate

**A1.** `./test` exits 0 with `ALL SUITES PASS`, with **at least** the 648 checks `main` has today.
Not an exact number: this slice adds tests, so it rises.

**A2.** Each **negative** case below is a constructed failure fixture required to fail **and to name
the reason**, not a test asserting a boolean.

## Ordinary callers are untouched

**A3.** A `potluck fix` with no export flag still applies its change to the operator's working tree
on PASSED, writes nothing on any other outcome, and removes its worktree on every path. Proven by a
test over a temporary repository.

**A4.** `--allow-dirty`'s help text and its three effects — skip the clean-tree precondition, drop
the restore, log `tree_hygiene_disabled` — are unchanged, with a source-level check pinning the help
string so a future edit reddens.

## The export

**A5.** A PASSED run told to export writes, into the caller-supplied out-of-tree directory: the
manifest, the cumulative patch, the run's `station-run.json`, and the exact spec, acceptance and gate
inputs. Payload names are manifest-relative and every payload's digest appears in the manifest.

**A6.** The manifest names the original task base commit, the output tree identity and digest, the
cumulative patch digest, the station artifact digest, the repository identity, the task, run and round, and
the imported-result field as an **explicit absence**.

**A7.** The export writes **nothing** to the operator's working tree: a test compares the operator
tree, its index projection, `HEAD` and every ref before and after, and requires them identical.

**A8. The manifest is published last.** A directory holding payloads but no manifest is not
acceptable, and the refusal says which file is missing. (Constructed: delete the manifest.)

**A9.** A payload whose bytes do not match its manifest digest is refused, naming the file and both
digests. (Constructed: flip one byte.)

**A10.** The cumulative patch carries additions, deletions, an exec-bit change, a symlink, binary
bytes, and an untracked-non-ignored file; ignored paths stay out. Verified by applying it to a fresh
checkout of the base and comparing the resulting tree digest with the manifest's.

**A11.** A PASSED run whose export cannot be completed **fails** rather than reporting success:
the exit code, the JSON result and the human summary all say so, while the station's own outcome
is kept separately. (Constructed: make the export directory unwritable.)

**A12.** A **non-PASSED** run writes nothing to the operator's tree and leaves no acceptable export.

**A13.** A run whose worktree HEAD left the base (`head != base`) is **ineligible to export**, with
its existing reason text intact.

## Boundaries

**A14. `station-run.json` stays v1.** Its field set and descriptions are unchanged, `base_commit`
still names the run's starting HEAD, `output_tree_digest` still digests the full final tree, and no
new required field appears. **If the slice cannot be built without changing v1, it stops and says
so** — that is an acceptable outcome and better than a silent reinterpretation.

**A15. No worktree is intentionally retained**, and after every path — pass, fail, refusal — no
`.<repo>.potluck-run-*` directory and no `.git/worktrees` entry for it remains. Pre-existing
hard-kill orphan gaps are declared, not closed here.

**A16. One repository.** No file outside this checkout is modified. Verified after the run against
every sibling repo's recorded HEAD, because potluck's restore is single-repo and a foreign edit would
escape it silently.

## Out of scope — do not do these here

Import, delivery, the delivery-failure message repair, the `mkstemp` fallback's removal, and any
change to restore or resume. Parts 2 and 3. A run not told to export keeps today's delivery behaviour
exactly.
