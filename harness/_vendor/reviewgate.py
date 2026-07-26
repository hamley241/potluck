#!/usr/bin/env python3
"""reviewgate — the pre-push review-authorization gate (CANONICAL source).

Stdlib only, Python >= 3.9 (the floor). This file runs as a git `pre-push` hook,
BEFORE any venv or toolchain exists, so it may import nothing outside the standard
library and use nothing newer than 3.9.

Principle (amended 2026-07-21): authored in the harness_core REPO but OUTSIDE the
engine package — it lives in `reviewgate/`, NOT `harness_core/`. Deliberately: the
engine's identity governance (EXPORT-MANIFEST + core_tests) then never sees a
non-engine module, so "every module in the importable package was extracted from
proven code" stays an ABSOLUTE claim with zero exceptions (guarantee ladder:
unrepresentable > validated — placing the tool where the invariant structurally
can't see it beats adding the invariant's first exception). Importability was
never load-bearing here: vendoring copies bytes, the hook executes the vendored
file directly, and the drift-check hashes bytes — nothing imports this module.
It is vendored byte-identical into each consumer repo's `harness/_vendor/
reviewgate.py`; never edit a vendored copy — change this file and re-vendor. A
consumer's suite hash-checks its copy against this canonical source (drift check).
The harness_core repo hosts TWO independently reviewed surfaces — the extracted
engine (EXPORT-MANIFEST) and this gate (its v5 spec + docs/reviews receipts);
neither certifies the other (see reviewgate/GOVERNANCE.md).

It implements the ratified Step-0 §A algorithm (see the repo's
docs/reviews/reviewgate-step0-reconciliation record). Summary of the guarantee:
a push is REFUSED unless, for every ref update it carries, either the changed
paths are entirely under the receipts directory (the regress exemption) or a
current, shape-valid, SHIP-class, provider-independent review receipt rides in
the pushed commits naming the tip's parent. Every git/schema sub-computation
that errors fails CLOSED (exit 2) — a non-answer is never approval.

Entry points:
  * (no subcommand) — the pre-push hook: reads ref-update lines on stdin,
    argv = [remote_name, push_url].
  * --install [--hooks=compose|repo-local] — install this as the repo's pre-push.
  * --doctor — report install + hooksPath regime, change nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

MIN_PY = (3, 9)
RECEIPTS_PREFIX = "docs/reviews/"
EXIT_REFUSE = 2  # fail-closed / refusal: a non-answer is never approval.

# --- §4 station-provenance (COMPILED opt-in, enforced NOWHERE initially) ------
# STATION_ENFORCED is a COMPILED map {remote-URL identifier -> frozen legacy boundary
# commit OID} — the §8b-allowlist posture (like FLOOR_PRINCIPAL / LEGACY_ALLOWLIST):
# activation and the boundary live in THIS reviewed-and-vendored code, NOT in the
# pushed content, so a pusher can neither enable/disable enforcement nor move the
# boundary in the very commit being checked (codex slice-3 BLOCKING-1/2). It is EMPTY
# here: enforced NOWHERE, so this gate — active for its own landing via the working-tree
# vendored copy — is inert in every repo (NON-CIRCULAR by construction). A product
# repo is added, with its frozen boundary OID, in a REVIEWED CODE CHANGE (re-vendored),
# ONLY once the conductor can feed a station_run. The key matches the push URL by exact
# string or trailing-substring (a repo's canonical remote); "" boundary = no legacy
# grandfathering (every landing must carry a station_run).
STATION_ENFORCED = {}  # e.g. {"github.com/hamley241/derma-demo": "<frozen-boundary-oid>"}
# The station artifact canon reviewgate recomputes (potluck's StationRunArtifact,
# conductor Step-0 §4f). A different/unknown canon OR schema fails closed. The schema
# version is pinned to potluck's Literal[1]; a v2 artifact (a new frame) requires a
# reviewed verifier bump, never a silent v1 read (the frozen-frame ruling).
STATION_ARTIFACT_CANON = "potluck-station-run-v1"
STATION_ARTIFACT_SCHEMA_VERSION = 1
# The station's ATTESTED terminal outcome that authorizes a landing. The station is a
# gate: only a run that PASSED authorizes. A failed/escalated (or absent) terminal_state
# REFUSES — the value is trusted (§4(f) declared limitation, threat-model B: a deliberate
# fabricator could lie), but an HONEST station's non-passed outcome must never land.
STATION_TERMINAL_PASSED = "passed"


# --------------------------------------------------------------------------- #
# fail-closed plumbing
# --------------------------------------------------------------------------- #

def _die(message: str, code: int = EXIT_REFUSE) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write("reviewgate: " + message.rstrip() + "\n")
    raise SystemExit(code)


def _check_interpreter() -> None:
    if sys.version_info < MIN_PY:
        _die(
            "this hook requires Python >= %d.%d and found %d.%d. It runs before any "
            "venv exists, so install a conforming python3 on PATH and retry."
            % (MIN_PY[0], MIN_PY[1], sys.version_info[0], sys.version_info[1])
        )


def _git(args, *, cwd=None, allow_fail=False, input_text=None):
    """Run `git <args>` and return stdout (str). On failure: refuse (exit 2),
    UNLESS allow_fail — then return None. `allow_fail` is used ONLY for the
    normalization-drop restrictive rule and for probing object availability,
    never on the approval side (a failed approval computation must refuse)."""
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:  # git not on PATH, etc.
        if allow_fail:
            return None
        _die("could not run git %s: %s" % (" ".join(args), exc))
    if proc.returncode != 0:
        if allow_fail:
            return None
        _die(
            "git %s failed (exit %d): %s"
            % (" ".join(args), proc.returncode, proc.stderr.strip())
        )
    return proc.stdout


def _zero_oid() -> str:
    """The all-zero object id, width-agnostic (sha1 or sha256). The width is that
    of a real object id; the empty-blob hash is the cheapest to obtain and its
    length is the id width for this repo's object format."""
    empty_blob = _git(["hash-object", "-t", "blob", "--stdin"], input_text="")
    return "0" * len(empty_blob.strip())


# --------------------------------------------------------------------------- #
# receipt shape-check (§E) — existence and shape only, no verdict-content parsing
# --------------------------------------------------------------------------- #

# The §E machine-checkable fields. `findings` is required (verbatim, or a
# same-repo pointer) — a receipt that authorizes non-receipt content with NO
# review evidence is exactly the hole a missing-findings check leaves open.
_REQUIRED_FIELDS = (
    "reviewer.model",
    "reviewer.provider",
    "doer.provider",
    "reviewed_head",
    "verdict",
    "date",
    "findings",
)


def parse_receipt(text: str):
    """Parse and shape-check a receipt's machine-checkable front-matter block per
    §E. The block is a leading fenced ```reviewgate ... ``` section of `key:
    value` lines (dotted keys allowed). Returns the validated dict, or raises
    ValueError naming the first shape problem. Existence-and-shape only: it never
    interprets the verdict's prose — only that §E's declared fields are present
    and well-formed."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "```reviewgate":
            start = i + 1
            break
    if start is None:
        raise ValueError("no ```reviewgate front-matter block found")
    fields = {}
    closed = False
    for ln in lines[start:]:
        if ln.strip() == "```":
            closed = True
            break
        if not ln.strip():
            continue
        if ":" not in ln:
            raise ValueError("malformed front-matter line (no ':'): %r" % ln)
        key, _, val = ln.partition(":")
        fields[key.strip()] = val.strip()
    if not closed:
        raise ValueError("unterminated ```reviewgate block")
    for req in _REQUIRED_FIELDS:
        if not fields.get(req):
            raise ValueError("receipt missing required field %r (§E)" % req)
    # date — validated BY CONSTRUCTION (calendar validity), not by a regex that
    # merely resembles a date: `2026-99-99` matches \d{4}-\d{2}-\d{2} but is not a
    # real date, and `time.strptime` is the mechanism that rejects it.
    try:
        time.strptime(fields["date"], "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            "receipt `date` is not a valid calendar date: %r" % fields["date"])
    # findings — verbatim prose (contains whitespace) OR a same-repo pointer.
    # Containment is checked BY CONSTRUCTION via normpath — a leading-`..` test
    # only resembles containment, and a mid-path `docs/../../other` escapes too.
    # A URL/scp form carries `:`; an absolute path or a normalized `..` escapes.
    findings = fields["findings"].strip()
    if not any(ch.isspace() for ch in findings):
        norm = os.path.normpath(findings)
        escapes = (os.path.isabs(findings) or norm == ".."
                   or norm.startswith(".." + os.sep))
        if ":" in findings or escapes:
            raise ValueError(
                "receipt `findings` pointer is not a same-repo relative path "
                "(no URL/scp `:`, no absolute path, no parent-escaping): %r"
                % findings)
    return fields


def _is_ship(verdict: str) -> bool:
    """SHIP-class verdicts. HOLD/INCOMPLETE are evidence, not authorization."""
    return verdict.strip().upper().split()[0] in ("SHIP",)


# --------------------------------------------------------------------------- #
# §A — pre-push authorization
# --------------------------------------------------------------------------- #

def _authoritative_commit_exclusions(push_url: str):
    """The remote's existing refs, normalized to a commit-exclusion set (§A Setup).

    `git ls-remote <push-url>` is authoritative for the destination; its failure
    refuses (a non-answer is never approval). Each advertised oid is peeled to a
    commit if it locally resolves to one; an oid that is a non-commit object OR is
    not present locally (ls-remote transfers no objects) is DROPPED. Dropping is a
    defined RESTRICTIVE rule, not an error path: it only enlarges the pushed set,
    so the failure direction is over-refusal, never under-authorization."""
    raw = _git(["ls-remote", push_url])  # failure => refuse
    exclusions = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        oid = parts[0].strip()
        peeled = _git(["rev-parse", "--verify", "--quiet", oid + "^{commit}"],
                      allow_fail=True)
        if peeled and peeled.strip():
            exclusions.append(peeled.strip())
        # else: not a commit, or object absent locally -> DROP (restrictive rule).
    return exclusions


def _pushed_set(tip: str, remote_oid: str, zero: str, exclusions):
    """The commits this ref update newly publishes (§A.3), as a list of oids."""
    if remote_oid == zero:
        args = ["rev-list", tip]
    else:
        # Update: use remote_oid..tip if remote_oid is available locally; else the
        # same restrictive fallback as a new ref (conservatively larger).
        have = _git(["rev-parse", "--verify", "--quiet", remote_oid + "^{commit}"],
                    allow_fail=True)
        if have and have.strip():
            out = _git(["rev-list", remote_oid + ".." + tip])
            return [c for c in out.split() if c]
        args = ["rev-list", tip]
    if exclusions:
        args = args + ["--not"] + exclusions
    out = _git(args)
    return [c for c in out.split() if c]


def _changed_paths(commits):
    """Union of paths changed by `commits` (§A.5): full diff per commit against
    each parent (`-m`) and against the empty tree for roots (`--root`)."""
    paths = set()
    for c in commits:
        out = _git(["diff-tree", "-r", "--no-commit-id", "--name-only", "-m",
                    "--root", c])
        for p in out.splitlines():
            if p.strip():
                paths.add(p.strip())
    return paths


def _set_has_root(commits):
    """True iff any commit in the set is a root (no parents) — a history-
    establishing push (§A.7 root-in-S guard)."""
    for c in commits:
        parents = _git(["rev-list", "--parents", "-n", "1", c]).split()
        if len(parents) == 1:  # just the commit itself, no parents listed
            return True
    return False


def _first_parent(tip: str):
    parents = _git(["rev-list", "--parents", "-n", "1", tip]).split()
    return parents[1] if len(parents) > 1 else None


def _path_present_at(rev: str, path: str) -> bool:
    """True if `path` is present in `rev`'s tree, False if CLEANLY absent. Any git
    error (bad rev, corruption, I/O) fails CLOSED via `_git` (exit 2) — a failed
    presence computation is never read as 'absent', which would let a receipt
    masquerade as newly added (§A exhaustive fail-closed; code-review finding 2).
    `ls-tree` returns the path when present and empty output when absent, both at
    rc 0 for a valid rev; an invalid rev is a non-zero rc that `_git` refuses on."""
    out = _git(["ls-tree", "-r", "--name-only", rev, "--", path])
    return bool(out.strip())


def _receipt_added_by_tip(path: str, tip: str, parent: str) -> bool:
    """The receipt must be ADDED (git status A) by the tip commit: present in the
    tip, absent in its parent (§A.7a). Binds a receipt to a single child and
    defeats stale reuse. Both lookups fail closed on any git error."""
    return _path_present_at(tip, path) and not _path_present_at(parent, path)


def _receipt_paths_at(tip: str):
    """docs/reviews/*.md paths present in the tip's tree."""
    out = _git(["ls-tree", "-r", "--name-only", tip, "--", RECEIPTS_PREFIX])
    return [p for p in out.splitlines() if p.strip().endswith(".md")]


# --------------------------------------------------------------------------- #
# §4 station-provenance verifier (opt-in)
# --------------------------------------------------------------------------- #

def _git_bytes(args):
    """Run `git <args>` and return stdout as RAW BYTES, fail-closed. Distinct from
    `_git` (which decodes text, corrupting non-UTF-8 pathnames): the station
    output-tree digest is sha256 over the exact bytes of `git ls-tree -r -z`, and a
    filename can be arbitrary bytes, so the digest must be recomputed over raw bytes to
    byte-match potluck's producer."""
    try:
        proc = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except OSError as exc:
        _die("could not run git %s: %s" % (" ".join(args), exc))
    if proc.returncode != 0:
        _die("git %s failed (exit %d): %s"
             % (" ".join(args), proc.returncode, proc.stderr.decode(errors="replace").strip()))
    return proc.stdout


def _ls_tree_digest(tree_ish: str) -> str:
    """sha256 over `git ls-tree -r -z <tree-ish>` — the SAME fingerprint potluck's
    station artifact uses for `output_tree_digest`, so a committed tree recomputes to
    the value the station attested when they are the same content."""
    return hashlib.sha256(_git_bytes(["ls-tree", "-r", "-z", tree_ish])).hexdigest()


def _sha256_text(s: str) -> str:
    """sha256 over `s.encode(utf-8, replace)` — potluck's `checkpoint.digest_text`, so a
    committed input file recomputes to the station artifact's spec/acceptance digest."""
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def _station_enforced(push_url):
    """(enforced, boundary) for `push_url` from the COMPILED STATION_ENFORCED map. A key
    matches when it is a substring of the push URL (a repo's canonical remote
    identifier). Enforced NOWHERE while the map is empty; a pusher cannot change the
    map (it is compiled + vendored). `boundary` "" means no legacy grandfathering."""
    for key, boundary in STATION_ENFORCED.items():
        if key and key in push_url:
            return True, boundary
    return False, None


def _station_legacy_exempt(reviewed_head, boundary) -> bool:
    """The FROZEN legacy exemption (conductor Step-0 §4): a field-less receipt is
    grandfathered ONLY when its REVIEWED content is an ANCESTOR of the COMPILED boundary
    OID (from STATION_ENFORCED — NOT from the pushed tip, codex slice-3 BLOCKING-2). An
    empty boundary grandfathers nothing. Objects absent locally / not-an-ancestor =>
    `--is-ancestor` returns non-zero => NOT exempt (enforce): fail-closed, never granting
    the exemption on an unresolved or non-ancestor boundary."""
    if not boundary:
        return False
    res = _git(["merge-base", "--is-ancestor", reviewed_head, boundary],
               allow_fail=True)
    return res is not None  # None on non-zero/absent-object -> enforce (fail closed)


def _committed_text(tip, path, ref_label):
    if not _path_present_at(tip, path):
        _die("refusing %s: station provenance names %r, absent from the pushed tip."
             % (ref_label, path))
    return _git(["cat-file", "-p", "%s:%s" % (tip, path)])


def _same_repo_pointer(ref, ref_label, what):
    """Reject a URL/scp `:`, an absolute path, or a `..`-escaping pointer (the
    `findings`-pointer containment rule)."""
    norm = os.path.normpath(ref)
    if (":" in ref or os.path.isabs(ref) or norm == ".."
            or norm.startswith(".." + os.sep)):
        _die("refusing %s: %s is not a same-repo relative path: %r"
             % (ref_label, what, ref))
    return ref


def _station_run_resolves(fields, tip, reviewed_head, ref_label) -> None:
    """A present `station_run` field must RESOLVE against recomputed git evidence, or
    `_die`. `station_run` names a committed station artifact (potluck's
    StationRunArtifact JSON) and `station_bundle` the committed handoff-bundle directory
    (menu handoff's spec.md / acceptance.md / verify.sh — the REAL landed format).
    reviewgate recomputes, trusting git not the record:

      * the artifact canon is known (fail closed on unknown);
      * `output_tree_digest` == the ls-tree digest of the landed station-output tree
        (`reviewed_head^{tree}`) — the landed code IS this run's output;
      * `base_commit` == that commit's own parent — committed on the base it ran from;
      * the artifact's INPUT digests bind to the committed bundle (codex slice-3
        BLOCKING-3): `spec_digest`/`acceptance_digest` == sha256 of the committed
        spec.md/acceptance.md (stripped, as potluck read them) and `gate_digest` ==
        sha256 of the committed verify.sh BYTES — so an artifact from a DIFFERENT run's
        inputs cannot be substituted under the same base+tree (§4(e)(4)).

    DECLARED PRODUCER-FIRST GAP (decider ruling 2026-07-26): the §4 tuple's
    `conductor_record_digest` — binding the whole task to the conductor's own run record
    — is NOT verified here, because its producer (the conductor's record format) does
    not exist yet. Building against a hypothesized format is the very producer-first
    hazard the ordering ruling forbids. Its closure is written into the conductor
    slice's completion requirements. `schema_version`/`canon_version` are pinned
    fail-closed (a new frame needs a reviewed verifier bump), and `terminal_state` must
    be "passed" — the station is a GATE, so a failed/escalated/absent outcome REFUSES;
    the value is trusted, not recomputed (§4(f) declared limitation, threat-model B)."""
    ref = _same_repo_pointer(fields["station_run"].strip(), ref_label, "station_run")
    if not fields.get("station_bundle"):
        _die("refusing %s: station_run is present but station_bundle (the committed "
             "handoff-bundle dir) is not — the run's inputs cannot be bound." % ref_label)
    bundle = _same_repo_pointer(fields["station_bundle"].strip(), ref_label, "station_bundle")
    blob = _git(["cat-file", "-p", "%s:%s" % (tip, ref)], allow_fail=True)
    if blob is None:
        _die("refusing %s: station_run points at %r, absent from the pushed tip."
             % (ref_label, ref))
    try:
        art = json.loads(blob)
    except ValueError as e:
        _die("refusing %s: station_run artifact %r is not valid JSON: %s"
             % (ref_label, ref, e))
    if not isinstance(art, dict) or art.get("canon_version") != STATION_ARTIFACT_CANON:
        _die("refusing %s: station_run artifact canon is %r, not %r (fail closed)."
             % (ref_label, art.get("canon_version") if isinstance(art, dict) else None,
                STATION_ARTIFACT_CANON))
    if art.get("schema_version") != STATION_ARTIFACT_SCHEMA_VERSION:
        _die("refusing %s: station_run artifact schema_version is %r, not %r — a new "
             "frame requires a reviewed verifier bump, never a silent v1 read (fail "
             "closed)." % (ref_label, art.get("schema_version"),
                           STATION_ARTIFACT_SCHEMA_VERSION))
    if art.get("terminal_state") != STATION_TERMINAL_PASSED:
        _die("refusing %s: station_run terminal_state is %r, not %r — the station is a "
             "gate, and only a run that PASSED authorizes a landing (a failed/escalated "
             "or absent outcome refuses; §4(f) attested)."
             % (ref_label, art.get("terminal_state"), STATION_TERMINAL_PASSED))
    # output tree + base
    tree = _git(["rev-parse", "--verify", "%s^{tree}" % reviewed_head]).strip()
    got = _ls_tree_digest(tree)
    if art.get("output_tree_digest") != got:
        _die("refusing %s: station_run output_tree_digest %r != the landed tree digest "
             "%r at reviewed_head %s — the landed code is not this station run's output."
             % (ref_label, art.get("output_tree_digest"), got, reviewed_head[:12]))
    base_parent = _first_parent(reviewed_head)
    if art.get("base_commit") != base_parent:
        _die("refusing %s: station_run base_commit %r != reviewed_head's parent %r — "
             "the station output is not committed on the base it ran from."
             % (ref_label, art.get("base_commit"),
                base_parent[:12] if base_parent else None))
    # input binding vs the committed handoff bundle
    for name, dig_field, digest in (
        ("spec.md", "spec_digest",
         lambda: _sha256_text(_committed_text(tip, bundle + "/spec.md", ref_label).strip())),
        ("acceptance.md", "acceptance_digest",
         lambda: _sha256_text(_committed_text(tip, bundle + "/acceptance.md", ref_label).strip())),
        ("verify.sh", "gate_digest",
         lambda: hashlib.sha256(
             _git_bytes(["cat-file", "-p", "%s:%s/verify.sh" % (tip, bundle)])).hexdigest()),
    ):
        if not _path_present_at(tip, bundle + "/" + name):
            _die("refusing %s: committed handoff bundle is missing %s/%s."
                 % (ref_label, bundle, name))
        want = digest()
        if art.get(dig_field) != want:
            _die("refusing %s: station_run %s %r != the committed %s/%s digest %r — the "
                 "artifact's inputs are not this bundle's (unrelated-run substitution)."
                 % (ref_label, dig_field, art.get(dig_field), bundle, name, want))


def _authorize_update(tip, remote_oid, zero, exclusions, ref_label, push_url):
    """Authorize one ref update, or _die refusing (§A per-line)."""
    commits = _pushed_set(tip, remote_oid, zero, exclusions)
    if not commits:
        return  # nothing new published (delete-noop / already-reachable rewind).
    paths = _changed_paths(commits)
    if not paths:
        return  # content-empty commits publish nothing reviewable.
    if all(p.startswith(RECEIPTS_PREFIX) for p in paths):
        return  # regress exemption: receipts-only push.
    # Non-receipt content present -> a receipt is required.
    if _set_has_root(commits):
        _die(
            "refusing %s: this push establishes history from nothing and carries "
            "non-receipt content. A first push to a virgin remote may contain ONLY "
            "the receipts directory — push a %s scaffold commit first, then push "
            "your content normally (a receipt riding in the same push does not "
            "authorize history-from-nothing)." % (ref_label, RECEIPTS_PREFIX)
        )
    parent = _first_parent(tip)
    if parent is None:
        _die("refusing %s: tip has no parent and non-receipt content." % ref_label)
    for rpath in _receipt_paths_at(tip):
        if not _receipt_added_by_tip(rpath, tip, parent):
            continue
        blob = _git(["cat-file", "-p", "%s:%s" % (tip, rpath)])
        try:
            fields = parse_receipt(blob)
        except ValueError:
            continue
        if not _is_ship(fields["verdict"]):
            continue
        if fields["reviewed_head"].strip() != parent:
            continue
        if fields["reviewer.provider"].strip().lower() == \
                fields["doer.provider"].strip().lower():
            continue
        # §4 station-provenance (COMPILED opt-in). When THIS repo is in the compiled
        # STATION_ENFORCED map (keyed on the push URL — not on pushed content, so a
        # pusher cannot toggle it), the landing must be the output of a station run: the
        # SHIP receipt must carry a `station_run` field that RESOLVES against recomputed
        # git evidence, or the reviewed content must be a grandfathered pre-boundary
        # landing (ancestor of the COMPILED boundary). `parent` == fields["reviewed_head"]
        # — the station-output commit. The map is EMPTY, so this is enforced NOWHERE: the
        # verifier running from the working-tree copy at its OWN 3-repo landing is inert
        # (non-circular by construction).
        enforced, boundary = _station_enforced(push_url)
        if enforced:
            if fields.get("station_run"):
                _station_run_resolves(fields, tip, parent, ref_label)  # _die on mismatch
            elif not _station_legacy_exempt(parent, boundary):
                _die("refusing %s: station-run provenance is required in this repo but "
                     "this receipt carries no station_run field and the reviewed content "
                     "is past the frozen legacy boundary. A product landing must be the "
                     "output of a station run." % ref_label)
        return  # authorized.
    _die(
        "refusing %s: no valid review receipt in %s names the tip's parent (%s), is "
        "SHIP-class, and has a reviewer provider different from the doer. Add one and "
        "commit it with the change, or push only receipts." % (
            ref_label, RECEIPTS_PREFIX, parent[:12])
    )


def run_hook(argv, stdin_text) -> int:
    _check_interpreter()
    push_url = argv[1] if len(argv) > 1 else (argv[0] if argv else "")
    if not push_url:
        _die("pre-push hook invoked without a destination URL argument.")
    zero = _zero_oid()
    exclusions = _authoritative_commit_exclusions(push_url)
    for line in stdin_text.splitlines():
        if not line.strip():
            continue
        cols = line.split()
        if len(cols) != 4:
            _die("malformed pre-push stdin line: %r" % line)
        local_ref, local_oid, remote_ref, remote_oid = cols
        ref_label = "%s -> %s" % (local_ref, remote_ref)
        if local_oid == zero:
            continue  # deletion publishes nothing.
        tip = _git(["rev-parse", "--verify", "--quiet", local_oid + "^{commit}"],
                   allow_fail=True)
        if not tip or not tip.strip():
            _die("refusing %s: %s does not peel to a commit (unsupported tag "
                 "target)." % (ref_label, local_oid[:12]))
        _authorize_update(tip.strip(), remote_oid, zero, exclusions, ref_label,
                          push_url)
    return 0


# --------------------------------------------------------------------------- #
# installer / doctor (§B) — thin; the heavy logic is above
# --------------------------------------------------------------------------- #

def _default_hooks_dir() -> str:
    # Absolute so the installed shim's chained-hook path is robust regardless of
    # the cwd at push time, and so identity comparisons are path-consistent.
    git_dir = _git(["rev-parse", "--absolute-git-dir"]).strip()
    return os.path.join(git_dir, "hooks")


def _effective_hooks_dir() -> str:
    return os.path.abspath(_git(["rev-parse", "--git-path", "hooks"]).strip())


def _our_shim_variants(vendored: str, hooks_dir: str) -> list:
    """The byte-exact shim(s) we would have written for THIS vendored path, as
    ENCODED BYTES — the no-chain form, and the chain form if a chained prior hook
    is present. 'Ours' is defined BY CONSTRUCTION (byte-exact equality with what
    we generate), never by resemblance. Identity is decided on BYTES, not text:
    reading/writing in text mode normalizes newlines (CRLF->LF), which would make
    a CRLF twin of our shim compare equal — text-exact, not byte-exact. Per obs
    019, a construction check is only as exact as the least-exact layer it passes
    through, so it extends to the I/O layer: bytes in, bytes out, bytes compared.

    Both variants are DETERMINISTIC from `hooks_dir`, so the identity set is a pure
    function of its construction inputs and NEVER consults the filesystem for which
    to recognize. A state-dependent set was the third face of obs 019: the installed
    shim can be the chain form, and if the chained file is later removed a
    conditional set would drop that variant, misclassify the installed shim as
    foreign, and recursively self-chain on reinstall. Both variants, always."""
    chained = os.path.join(hooks_dir, "pre-push.reviewgate-chained")
    return [_shim_text(vendored, None).encode("utf-8"),
            _shim_text(vendored, chained).encode("utf-8")]


def _is_exact_ours(content: bytes, vendored: str, hooks_dir: str) -> bool:
    return content in _our_shim_variants(vendored, hooks_dir)


def run_doctor() -> int:
    _check_interpreter()
    default = _default_hooks_dir()
    effective = _effective_hooks_dir()
    is_default = os.path.abspath(effective) == os.path.abspath(default)
    regime = ("default (.git/hooks)" if is_default
              else "non-default core.hooksPath: %s" % effective)
    vendored = os.path.abspath(__file__)
    target = os.path.join(effective, "pre-push")
    content = b""
    if os.path.exists(target):
        try:
            with open(target, "rb") as fh:
                content = fh.read()
        except OSError:
            content = b""
    # Identity in the CONSUMER's terms: git runs a hook iff it is an EXECUTABLE
    # file at this path, so 'installed' requires byte-exact content AND +x — a
    # byte-exact but non-executable hook is a silently disabled gate.
    content_ours = os.path.exists(target) and _is_exact_ours(content, vendored, effective)
    executable = os.path.exists(target) and os.access(target, os.X_OK)
    gate_installed = content_ours and executable
    foreign_present = os.path.exists(target) and not content_ours
    chained_present = os.path.exists(os.path.join(effective, "pre-push.reviewgate-chained"))
    if gate_installed:
        installed = "yes"
    elif content_ours and not executable:
        installed = "no (present but NOT executable — git skips it; re-run setup)"
    else:
        installed = "no"
    sys.stdout.write("reviewgate doctor:\n")
    sys.stdout.write("  hooksPath regime         : %s\n" % regime)
    sys.stdout.write("  reviewgate installed     : %s\n" % installed)
    sys.stdout.write("  foreign pre-push present : %s\n" % ("yes" if foreign_present else "no"))
    sys.stdout.write("  chained prior hook       : %s\n" % ("yes" if chained_present else "no"))
    return 0


def run_install(hooks_flag) -> int:
    _check_interpreter()
    default = _default_hooks_dir()
    effective = _effective_hooks_dir()
    is_default = os.path.abspath(effective) == os.path.abspath(default)
    if is_default:
        return _install_into(default)
    if hooks_flag == "compose":
        return _install_into(effective)
    if hooks_flag == "repo-local":
        _git(["config", "--local", "core.hooksPath", default])
        return _install_into(default)
    _die(
        "a non-default core.hooksPath is in force (%s), so reviewgate will not "
        "silently install outside it or silently disable it. Re-run with ONE of:\n"
        "  --hooks=compose     install/compose into that effective path\n"
        "  --hooks=repo-local  set a repo-local core.hooksPath override to this "
        "repo's %s (this HIDES your global hooks for THIS repo), then install there"
        % (effective, default)
    )


def _install_into(hooks_dir: str) -> int:
    """Install reviewgate as the repo's pre-push. If a foreign non-sample pre-push
    already exists, PRESERVE it by chaining (§C): the shim buffers stdin, runs
    reviewgate, then replays the buffered stdin to the prior hook with args
    forwarded and its exit status propagated. Never overwrites a real hook's
    behavior; idempotent for our own shim."""
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-push")
    vendored = os.path.abspath(__file__)
    chained_path = os.path.join(hooks_dir, "pre-push.reviewgate-chained")
    if os.path.exists(target):
        try:
            with open(target, "rb") as fh:
                existing = fh.read()
        except OSError:
            existing = b""
        # TWO rows, no third category (finding 6, decider-endorsed). Either the
        # existing pre-push is BYTE-EXACT ours -> idempotent skip; or it is
        # anything-else (a foreign hook, OR a non-byte-exact own shim, which is
        # indistinguishable from foreign BY CONSTRUCTION) -> PRESERVE + chain,
        # never overwrite. No path can destroy an unrecognized hook.
        if _is_exact_ours(existing, vendored, hooks_dir):
            # Self-heal a lost executable bit (git skips non-executable hooks), and
            # SAY SO — a repaired gate is an event worth a line (visible handling).
            if not os.access(target, os.X_OK):
                os.chmod(target, 0o755)
                sys.stdout.write("reviewgate: restored executable bit on %s\n" % target)
            sys.stdout.write("reviewgate: pre-push already installed at %s\n" % target)
            return 0
        if os.path.exists(chained_path):
            _die("cannot compose: %s already exists; resolve it by hand." % chained_path)
        os.rename(target, chained_path)
        _write_shim(target, vendored, chained_path)
        sys.stdout.write("reviewgate: installed pre-push at %s (prior hook preserved "
                         "+ chained -> %s)\n" % (target, chained_path))
        return 0
    _write_shim(target, vendored, None)
    sys.stdout.write("reviewgate: installed pre-push at %s\n" % target)
    return 0


def _write_shim(target: str, vendored: str, chained) -> None:
    # Binary write: the shim's bytes on disk ARE its identity (obs 019 addendum).
    with open(target, "wb") as fh:
        fh.write(_shim_text(vendored, chained).encode("utf-8"))
    os.chmod(target, 0o755)


def _shim_text(vendored: str, chained) -> str:
    lines = [
        "#!/bin/sh",
        "# reviewgate pre-push hook. Buffers stdin, runs reviewgate, then (if a",
        "# prior hook was chained) replays the buffered stdin to it with args",
        "# forwarded and its failure propagated. Authored by harness_core/reviewgate.py.",
        "set -e",
        'tmp="$(mktemp)"',
        "trap 'rm -f \"$tmp\"' EXIT",
        'cat > "$tmp"',
        'python3 %s "$@" < "$tmp"' % _shq(vendored),
    ]
    if chained:
        lines.append('if [ -x %s ]; then %s "$@" < "$tmp"; fi'
                     % (_shq(chained), _shq(chained)))
    lines.append("")
    return "\n".join(lines)


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--doctor":
        return run_doctor()
    if argv and argv[0] == "--install":
        flag = None
        for a in argv[1:]:
            if a.startswith("--hooks="):
                flag = a.split("=", 1)[1]
        return run_install(flag)
    # default: the pre-push hook. git passes [remote_name, url] as argv and the
    # ref-update lines on stdin.
    return run_hook(argv, sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
