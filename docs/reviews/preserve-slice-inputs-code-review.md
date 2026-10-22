# Preserve slice inputs — codex findings

## Round 1 — 1 BLOCKING, 1 MAJOR, both on the review driver

- **`driver-leaks-machine-absolute-paths` (BLOCKING)** — `/Users/gp` paths, a session temp path,
  a local checkout name and a session UUID. Violates A4 directly.
- **`driver-is-session-bound-not-reusable` (MAJOR)** — hardcodes one repository, one slice, one
  round and vanished inputs. It is scratch, not the "reusable infrastructure" A3 claimed.

**Resolution: the driver is dropped from the commit**, not fixed in place. The specs and
acceptance files — the records that actually close the receipts' gap — carry no machine paths
and land unchanged.

The finding landed on the one thing I had asked to be attacked, which is the argument for naming
the suspect part of a change in the review prompt rather than hoping it is noticed.
