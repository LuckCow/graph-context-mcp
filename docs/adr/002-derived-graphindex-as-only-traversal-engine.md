# ADR 002: A derived in-memory GraphIndex is the only traversal engine

**Status:** Accepted (backfilled)

## Context

Anytype's API can list and text-search objects but cannot answer graph
questions (neighbors, bounded BFS, shortest path). `explore` and `find_path`
are the product's core retrieval primitives. The spike (S2) showed a full
hydration of ~2,000 objects costs 2–3 `GET /objects` calls in well under a
second — cheap enough to rebuild from scratch on every startup.

## Decision

All traversal runs against `domain/graph.py::GraphIndex`, an in-memory
adjacency projection **derived** from the store. It is rebuildable, never
authoritative: repository adapters keep it coherent (write-through on our own
writes, hydrate/resync for human edits), and the write ordering is
persist-to-Anytype-first, index-second — the index may lag the store, never
lead it.

## Consequences

- Zero query latency and pure, exhaustively unit-testable traversal code.
- The index can be stale with respect to out-of-band edits between resyncs;
  deletions in particular are only reconciled on full hydrate (spike S4).
- Reverse adjacency exists only in the index (the store keeps edges on the
  source object only, ADR 003).
- At very large world sizes a persisted index snapshot may become worthwhile;
  the spike moved that trigger far out (reads are unthrottled, page size 1000).
