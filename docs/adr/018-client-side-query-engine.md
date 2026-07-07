# ADR 018: Attribute queries run client-side on the GraphIndex

Date: 2026-07-07
Status: accepted

## Context

The LLM had no Anytype-Set-like retrieval: `explore` walks outward from a
start node and `find_node` matches names, but "10 open Todos ordered by
due date then priority" or "all Events linked to this Character, in story
order" had no tool. Meanwhile the data was already in place: ADR 012
reflects every native scalar property into `Node.fields` (as strings) in
the in-memory `GraphIndex`, and ADR 002 established that index as the only
traversal engine.

The Anytype API does expose Sets server-side — sets and collections are
both "lists" (`GET .../lists/:list_id/views`,
`GET .../lists/:list_id/:view_id/objects`, the latter filtered and sorted
per the view) — but those endpoints have never been used by this codebase
and their behavior for query-layout lists is unverified (spike S9,
scheduled). `POST /search` also gained property filters in `2025-11-08`,
but it caps pages at 100, needs a round-trip per query, and cannot work on
the memory backend.

## Decision

**All attribute queries — ad-hoc predicates today, compiled Set views
tomorrow — execute on the derived `GraphIndex`, in the pure engine
`domain/query.py` (`run_query`).** This extends ADR 002: the index is the
only query engine, for traversal *and* for attribute scans. Anytype's
lists/views endpoints are, at most, a **view-definition source** (a saved
`where`+`order_by` to compile into a `NodeQuery`) — never a second query
engine. The `query` tool (interface layer) is the surface; `Querier`
(application) is the seam where saved-view resolution will land.

Semantic contracts the LLM builds habits on, so they are pinned by tests
and recorded here:

* **Values are strings** (ADR 012 reflection). Two values compare
  numerically when *both* parse as floats, otherwise casefolded
  lexicographically — ISO dates therefore order chronologically. The
  coercion rule lives in `domain/query.py` only.
* **`neq` matches absent fields** ("not known to be value"). An unticked
  Anytype checkbox is dropped as absence (quirk quarantined in
  `mapping.field_value`), so `done neq true` is the open-todos idiom.
  `eq`/`lt`/`lte`/`gt`/`gte`/`contains`/`exists` never match absence;
  `missing` matches only absence.
* Nodes missing a sort key order **last** regardless of direction; final
  tie-break is `(name, id)`, matching `find_by_name`.
* A referenced field that appears on **no** candidate (and is not a
  built-in) is an error listing the fields that do exist — zero-occurrence
  is a typo, per-node absence is ordinary sparseness.
* `linked_to` anchors the candidate set to one node's direct neighbors
  (both directions) — character timelines are
  `query(type="Event", linked_to=X, order_by=["story_time"])`. Deeper
  reach stays `explore`'s job (ADR 005 subtree-pruning semantics do not
  apply here; a query has no paths to prune).

## Consequences

* Works identically on the memory backend and the Anytype backend, stays
  in `tests/unit` at millisecond speed, and needs no new I/O: freshness
  rides the existing hydrate/resync machinery.
* Full-scan cost is accepted: worlds are thousands of nodes (ADR 002's
  scale argument); `overview` already scans. `limit` is clamped to 100.
* Body text is **not** queryable — bodies never enter the index (ADR 010).
  `contains` works on reflected scalar fields and summaries only.
* The gated fast-follow (after spike S9): a `view` parameter on the same
  tool, mutually exclusive with `type`/`where`/`order_by`/`linked_to`. If
  views expose machine-readable filter/sort definitions, they compile to
  `NodeQuery` and run through this engine; only if they don't (but the
  view-objects endpoint applies them server-side) does execution fall back
  to the API, mapped through `mapping.to_node` — and that fallback would
  amend this ADR.
