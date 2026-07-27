# Spike: fenced JSONL survives the body round trip (ADR 049)

Date: 2026-07-27
Status: resolved (live-confirmed)

## Question

The revision historian stores its log as JSON lines inside one fenced
block in a `gc_node_history` sidecar's body. The store normalizes
markdown on write (ADR 010 / S6; A8 summary prefix, A9 first-line
heading flatten, A13 fence-info-string drop) — do fence CONTENTS survive
`create(body=…)` → `fetch_body` byte-usable, i.e. parseable back as the
same JSON?

## Result

**Yes.** The contract test
`GraphRepositoryContract::test_fenced_jsonl_body_round_trips_intact`
(three compact-JSON records with quotes, `&`, and `*marks*` inside a
bare ``` fence) passes on the in-memory fake, the mock-backed adapter,
and — run 2026-07-27 via
`ANYTYPE_E2E=1 pytest tests/e2e/test_live_contract.py::TestAnytypeLiveRepository::test_fenced_jsonl_body_round_trips_intact`
— against the live headless sidecar: every line parses back to identical
JSON.

## Log-format defenses (belt and braces, pinned in domain/revisions.py)

* No info string on the fence (A13 would drop it).
* No heading-shaped first line (A9 would flatten it) — the log opens
  with a plain header sentence.
* `parse_log` is lenient: an unparseable line is skipped and counted,
  never fatal, so any future normalization quirk degrades history
  instead of bricking the historian.

## Open sibling question

Does the API expose a last-modified-by identity for objects? Until
answered, human revisions attribute to the generic `"human"`
(ADR 049).
