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

## Amendment (2026-07-07, ADR 019)

The premise widens rather than changes: Anytype is now also the CHAT
surface (WP14 — the bot converses inside the space via the chat API, and
its replies deep-link the objects it created), and the bot's own Anytype
node is becoming headless (anytype-cli sidecar with a bot account).
Humans keep the desktop app as their editing surface; "storage + human
surface" now reads "storage + human surface + conversation surface", all
one store.
