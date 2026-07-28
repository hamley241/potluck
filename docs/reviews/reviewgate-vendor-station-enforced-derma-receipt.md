# potluck — re-vendor reviewgate STATION_ENFORCED derma activation — receipt

Authorizes the byte-identical re-vendor of the RATIFIED canonical `reviewgate.py` into
`harness/_vendor/reviewgate.py`.

```reviewgate
reviewer.model: gpt-5.6-sol
reviewer.provider: openai
doer.provider: anthropic
reviewed_head: bf9679979bb0322c351d177b43b079a9eb6c290e
verdict: SHIP
date: 2026-07-27
findings: The vendored copy is BYTE-IDENTICAL to the canonical reviewgate.py ratified in harness_core (codex RATIFIED round 6, receipt harness_core/docs/reviews/reviewgate-station-enforced-derma-receipt.md); the drift-check test_reviewgate_vendor is green. Currency-only re-vendor of a reviewed canonical; inert in potluck (its remote is not the enforced derma identity).
```

**RATIFIED→SHIP** via the canonical's codex ratification + the byte-identity drift-check (obs-025
canonical-first). reviewed_head is the FULL 40-char OID.
