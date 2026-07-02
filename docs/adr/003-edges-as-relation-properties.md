# ADR 003: Edges are relation properties on the source object

**Status:** Accepted (backfilled) — amended by ADR 006 (which relations count)

## Context

Anytype has no first-class edge entity. Candidate encodings: (a) relation
("objects"-format) properties on the source object, (b) an edge-as-object
type whose name encodes the endpoints, (c) a JSON adjacency blob per node.
Only (a) is natural to edit in the Anytype UI — which is the point of using
Anytype at all (ADR 001). Spike S1 (the go/no-go gate) confirmed relation
properties round-trip: creatable via API, populatable at object creation,
modifiable via PATCH.

## Decision

A directed edge `A —type→ B` is stored as an entry in a relation property on
the **source** object A. One property per edge type. Reverse adjacency is not
stored; it exists only in the GraphIndex (ADR 002).

## Consequences

- Edges are visible and editable in the Anytype UI as ordinary relations.
- PATCH replaces a relation's target list wholesale (spike S1/A4), so link
  add/remove is read-modify-write — last-write-wins against concurrent human
  edits, logged loudly (WORK_PACKAGES Q2).
- Composite create needs rollback choreography instead of a transaction:
  archive the new node and restore patched sources on failure
  (`repository.py`).
- Originally the property set was a closed `gc_edge_*` vocabulary; ADR 006
  widened it to every non-system "objects"-format relation in the space.
