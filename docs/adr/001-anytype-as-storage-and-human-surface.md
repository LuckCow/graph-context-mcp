# ADR 001: Anytype as durable storage and the human editing surface

**Status:** Accepted (backfilled; embodied in code since WP0/WP1)

## Context

The story-world graph needs durable storage that a human author can also
browse and edit directly, without going through the LLM. Building a custom
store would mean building a custom editor too. Anytype provides local-first
storage, a polished editing UI, object types, and relation properties, plus a
documented local HTTP API (`http://localhost:31009/v1`, pinned via the
`Anytype-Version` header).

## Decision

Anytype is the single durable store *and* the human editing surface. The MCP
server owns no persistent state of its own; everything the server knows is
recoverable from the space (see ADR 002). Out-of-band human edits are
first-class: hydrate/resync pull them in rather than treating them as
corruption.

## Consequences

- The representation must stay human-editable — this constrains every mapping
  decision (ADR 003, ADR 006) and rules out opaque blobs for story data.
- The adapter inherits Anytype's API quirks (PATCH replaces relation lists,
  write-once bodies, archived objects invisible to search, ~1 write/s
  sustained rate limit). All of them are quarantined in
  `infrastructure/anytype/mapping.py` and pinned by `mock_server.py`.
- Deletion is archival; human deletions are invisible to incremental resync
  and require full-set reconciliation on hydrate (spike S4).
- Concurrency with human edits is last-write-wins for v1 (WORK_PACKAGES Q2).
