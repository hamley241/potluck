#!/usr/bin/env python3
"""PreToolUse tripwire.

Active on EVERY machine (base layer). It is cheap and silent on the normal path;
it only fires on the mistake case -- a secret or PII-shaped value about to leave
in a diff sent to an external reviewer (Codex/Kimi).

Rationale we agreed on: the code is assumed PII-free, but test fixtures, seed
files, recorded responses, and stray secrets are the classic accidental-leak
vector. The hook is not here because you plan to send sensitive data -- it is
here because the cost of being wrong once is high and the scan is nearly free.

It blocks (exit non-zero) rather than warns, because on an autonomous loop a
warning nobody reads is not a control.

Wire-up: register as a PreToolUse hook in settings.json on tools that send
content to external model CLIs. Reads the candidate payload on stdin.
"""

import re
import sys

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


def scan(text: str) -> list[str]:
    hits = []
    for pat, label in SECRET_PATTERNS:
        if re.search(pat, text):
            hits.append(f"SECRET: {label}")
    for pat, label in PII_PATTERNS:
        if re.search(pat, text):
            hits.append(f"PII: {label}")
    # de-dup while preserving order
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
            "\nThe external debate step has been blocked. Inspect the diff/fixtures, "
            "remove the sensitive value, or route this change to human-only review.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
