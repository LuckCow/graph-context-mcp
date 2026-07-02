# ADR 006: Space-reflecting open schema

**Status:** Accepted (2026-06-27) — supersedes the closed-vocabulary design in
WORK_PACKAGES WP1 and amends ADR 003

## Context

The original design minted a closed, parallel `gc_` vocabulary: one
`gc_character`/`gc_event`/… type per domain `NodeType` and one `gc_edge_*`
relation per domain `EdgeType`. Dogfooding against a real space showed this
fights the product premise (ADR 001): users already *have* types
(`character`, `event`, …) and relations (`boss`, `triggered_by`, …), and a
parallel schema duplicates their world and hides their own edits from the
graph.

## Decision

The system **reflects the user's existing Anytype space** instead of
maintaining its own vocabulary:

- **Nodes:** any non-archived object of any type. The node's `type` is the
  native type's display name; `type_key` is its raw key.
- **Edges:** every "objects"-format relation property on the source object —
  bootstrapped `gc_edge_*` and human-created relations alike — minus a small
  system denylist (`backlinks`, `creator`, `last_modified_by`).
- **Edge labels are derived from the property KEY** (`clean_label`: strip
  `gc_edge_`/`gc_` prefixes), not the display name. Key-derivation guarantees
  a label round-trips to the exact property on write and on filter.
- **Writes resolve, never invent:** a requested type or relation label is
  resolved against the live `SpaceRegistry` (built from `GET /types` +
  `GET /properties` per hydrate, refreshed on resync). An unknown type raises
  `UnknownNodeType`; an unknown relation label raises `UnknownRelationLabel`
  and is surfaced for approval — unless the caller passes
  `create_missing_relations=true`, in which case a new relation is created
  (per-object; Anytype relations are space-global and need not touch the
  type).
- **Semantics via roles, not types:** an editable type-key → `Role` map
  (`domain/schema.py::DEFAULT_TYPE_ROLES`, overridable per space through the
  registry) preserves type-aware behavior — Event/`story_time`/`as_of`
  timeline semantics, and hiding infra roles (Prose, SessionContext) from
  explore. An unmapped type is first-class but semantically neutral.
- **A thin `gc_` layer survives for infrastructure only:** the `gc_prose` and
  `gc_session_context` types, the scalar properties we write onto native
  objects (`gc_summary`, `gc_summary_stale`, `gc_description`,
  `gc_story_time`, `gc_fields`), and a starter vocabulary of `gc_edge_*`
  relations as reusable defaults.

## Consequences

- Human-created types and relations appear in the graph without ceremony;
  the LLM reuses the user's own vocabulary in errors and suggestions
  (`known_node_types` / `known_edge_labels`).
- The in-memory fake keeps an open vocabulary with no registry;
  `UnknownNodeType`/`UnknownRelationLabel` are Anytype-backend behaviors.
- Legacy read-compat: spaces touched by the old bootstrap contain duplicate
  `gc_character`/… types; the role map still resolves them so old objects
  keep their roles. Archiving those duplicate types is a manual, per-space
  cleanup.
- WORK_PACKAGES WP1/WP2 sections describing the fixed vocabulary are
  historical; see the status addendum there.
