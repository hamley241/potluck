#!/usr/bin/env python3
"""PreToolUse tripwire.

Active on EVERY machine (base layer). It is cheap and silent on the normal path;
it only fires on the mistake case -- a secret or PII-shaped value about to leave
in a diff sent to an external reviewer (Codex/Kimi).

Rationale: the code is assumed PII-free, but test fixtures, seed files, recorded
responses, and stray secrets are the classic accidental-leak vector. The hook
is not here because you plan to send sensitive data -- it is here because the
cost of being wrong once is high and the scan is nearly free.

It blocks (exit non-zero) rather than warns, because on an autonomous loop a
warning nobody reads is not a control.

DEFAULTS:
- Secret patterns: always ON. Cannot be disabled -- accidental secret leaks
  have unbounded blast radius.
- PII patterns: ON by default. Whitelist filters the common test fixtures
  (RFC 2606 example domains, Visa/MC test card numbers, SSA-reserved SSNs,
  fictional 555-01xx phone range) so legitimate test data doesn't fire.

OFF-SWITCH FOR PII (secrets stay on regardless):
A repo that is demonstrably PII-free can turn PII scanning off by committing a
`.claude/secret-scan.toml` at repo root:

    pii_enabled = false

Committed (not env var) so opting out is an explicit, attributable, on-the-
record decision. Safe default; visible opt-out.

Wire-up: `potluck setup` registers this as a PreToolUse hook in
~/.claude/settings.json for the tools that send content to external model CLIs.
Reads the candidate payload on stdin.
"""

import re
import sys
from pathlib import Path

# --- Patterns -----------------------------------------------------------------

# Secret patterns (high-precision prefixes -> low false-positive rate).
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI-style secret key"),
    (r"sk-ant-[A-Za-z0-9-]{16,}", "Anthropic API key"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub personal access token"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]",
     "inline credential assignment"),
]

# PII-shaped patterns. Deliberately conservative: better to occasionally block
# and let a human override than to leak. Tune per your data.
PII_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "US SSN-shaped value"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "email address"),
    (r"\b\d{16}\b", "16-digit number (possible PAN)"),
    (r"\bMRN[:#]?\s*\d{5,}\b", "medical record number"),
    (r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b", "card-number-shaped value"),
]

# Whitelist for common test fixtures -- values that MATCH a PII pattern but are
# canonical "not real data" strings. Filters false positives on the common
# testing-workflow pain points. Each entry is a full-match regex against the
# specific PII-pattern hit (not the whole payload).
PII_WHITELIST = [
    # RFC 2606 reserved test/example domains.
    r".+@(?:example\.(?:com|org|net|edu)|(?:.+\.)?example\.(?:com|org|net|edu))",
    # Well-known payment test PANs.
    r"4111\s?1111\s?1111\s?1111",
    r"4111-1111-1111-1111",
    r"5555\s?5555\s?5555\s?4444",  # Mastercard test
    r"5555-5555-5555-4444",
    r"4111111111111111",           # bare-digit forms
    r"5555555555554444",
    # SSA-reserved SSN ranges (never assigned to real individuals).
    r"000-\d{2}-\d{4}",
    r"666-\d{2}-\d{4}",
    r"9\d{2}-\d{2}-\d{4}",
    # Canonical example SSN used in training material and RFCs.
    r"123-45-6789",
]

# --- Config ------------------------------------------------------------------

_CONFIG_PATH = Path(".claude/secret-scan.toml")


def _pii_enabled() -> bool:
    """Read `.claude/secret-scan.toml` from CWD. Absent or malformed -> True
    (fail safe: scanning ON). Committed off-switch means opting out leaves an
    audit trail; env var was rejected precisely because it doesn't."""
    if not _CONFIG_PATH.exists():
        return True
    try:
        import tomllib
    except ImportError:  # pragma: no cover -- 3.10 compat path
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return True
    try:
        cfg = tomllib.loads(_CONFIG_PATH.read_text())
    except Exception:
        return True
    return bool(cfg.get("pii_enabled", True))


# --- Scan --------------------------------------------------------------------

def _is_whitelisted(matched: str) -> bool:
    for wp in PII_WHITELIST:
        if re.fullmatch(wp, matched):
            return True
    return False


def scan(text: str, pii_enabled: bool | None = None) -> list[str]:
    """Return a de-duplicated list of hit descriptions. Secret hits are always
    included; PII hits only when `pii_enabled` (default: read from config).
    A hit is emitted at most once per pattern even if the pattern matches
    multiple times -- the point is signalling that the pattern fired, not
    counting occurrences."""
    if pii_enabled is None:
        pii_enabled = _pii_enabled()
    hits: list[str] = []
    for pat, label in SECRET_PATTERNS:
        if re.search(pat, text):
            hits.append(f"SECRET: {label}")
    if pii_enabled:
        for pat, label in PII_PATTERNS:
            for m in re.finditer(pat, text):
                if not _is_whitelisted(m.group(0)):
                    hits.append(f"PII: {label}")
                    break  # one hit per pattern is enough
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def main() -> int:
    payload = sys.stdin.read()
    hits = scan(payload)
    if hits:
        sys.stderr.write(
            "BLOCKED by tripwire: outbound content matched sensitive patterns "
            "before being sent to an external reviewer.\n"
        )
        for h in hits:
            sys.stderr.write(f"  - {h}\n")
        sys.stderr.write(
            "\nThe external debate step has been blocked. Inspect the diff/"
            "fixtures, remove the sensitive value, route this change to "
            "human-only review, or (if the repo is demonstrably PII-free) "
            "add `pii_enabled = false` to .claude/secret-scan.toml.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
