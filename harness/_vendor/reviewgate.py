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

import os
import subprocess
import sys
import time

MIN_PY = (3, 9)
RECEIPTS_PREFIX = "docs/reviews/"
EXIT_REFUSE = 2  # fail-closed / refusal: a non-answer is never approval.


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


def _authorize_update(tip, remote_oid, zero, exclusions, ref_label):
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
        _authorize_update(tip.strip(), remote_oid, zero, exclusions, ref_label)
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
