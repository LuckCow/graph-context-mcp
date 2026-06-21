# Graph-Context MCP Tool — Development Proposal

## Overview

An MCP server backed by Anytype that lets an LLM reliably and efficiently build, query, and maintain a graph representation of a story world. The graph is the source of truth for characters, locations, events, technologies, and rendered prose. The LLM plays two roles: **world-builder** (translating user prompts into graph structure) and **renderer** (turning graph context into prose for scenes the user explores).

The central design problem is not storage but **retrieval shaped for the task**: keeping the right slice of the world inside the LLM's context window at the right time, with minimal tool calls.

## Design Principles

1. **Small, parameterized tool surface.** Target 6–8 general-purpose tools with rich parameters and sensible defaults, rather than many narrow tools. Scene assembly, neighbor exploration, and filtered traversals are all parameter configurations of the same primitives.
2. **Composite writes.** Creating and linking a node is one call, not three. Every write operation accepts optional links so common patterns never require round-trips.
3. **Context echo.** The server maintains session state (active project, focus stack) and echoes a compact context header in every tool response, keeping state inside the LLM's context window where it influences behavior.
4. **Typed everything.** Nodes and edges use a defined type vocabulary. Queries filter on types; path-finding traverses only meaningful edges. (Type extensibility is deferred — fixed vocabulary for v1.)
5. **Bounded responses.** Every query has depth and result-count limits with conservative defaults, so a mature graph never floods the context window.
6. **Stored summaries with staleness tracking.** Summaries are written on the node, not computed at query time. Updates auto-flag staleness unless a fresh summary accompanies the change.

## Architecture

- **Storage layer:** Anytype, accessed via its API. Each story world is an Anytype space (or a typed subtree within one).
- **MCP server:** mediates all reads/writes, owns session state, enforces schema, assembles query responses.
- **Session state:** held by the server during a session and **persisted as a meta-node in the graph** (type: `SessionContext`) so it survives restarts and lays groundwork for multi-user later. Single-user for v1; one active project at a time.

## Context Management

### State tracked
- **Active project** (story world / space).
- **Focus stack:** an ordered working set of the last N nodes touched or explicitly focused (default N = 6). Scene work involves several entities at once; a single "current node" pointer would thrash.
- **Recent history:** trailing list of recently visited nodes beyond the focus stack, for breadcrumb-style backtracking.

### Behavior
- Every tool response begins with a compact header, e.g.
  `[project: Ashfall | focus: Mira (Character), The Undercroft (Location), Siege of Brakk (Event) | recent: ...]`
- Server-side defaults: when a query omits a starting node, the top of the focus stack is used.
- Reads and writes automatically push the affected node onto the focus stack; explicit `focus` operations allow manual control (set, pin, clear).
- State changes are mirrored to the `SessionContext` meta-node on write.

## Schema (v1, fixed vocabulary)

**Node types:** `Character`, `Location`, `Event`, `Technology`, `Faction`, `Item`, `Prose`, `SessionContext`.

**Common fields:** `name`, `summary` (one-liner, required on create), `summary_stale` (bool), `description`, type-specific fields.

**Event fields:** `time` (required — position on the story timeline), `participants`, `location`.

**Prose fields:** rendered text, `references` (links to every node used to generate it), `llm_input` (the assembled context/prompt), `llm_output` (raw response), generation metadata (model, date). Prose lives in the graph so consistency checks ("how was this place described last time?") are queryable.

**Edge types (illustrative core set):** `knows`, `located_at`, `member_of`, `participated_in`, `caused`, `possesses`, `parent_of`/`child_of`, `references` (prose → source nodes), `precedes` (event ordering where explicit times tie).

## Tool Surface (target: 7 tools)

1. **`context`** — get/set active project; push, pin, pop, or clear focus entries. Returns full session state.
2. **`create_node`** — create a node of a given type with fields **and an optional list of typed links** in one call. Requires a `summary`. Returns the new node + context header.
3. **`update_node`** — modify fields and/or add/remove links. **Automatically sets `summary_stale = true` unless the update includes a new `summary`.**
4. **`get_node`** — in-depth retrieval: all fields, all edges (grouped by type), optionally the full text of linked Prose. Parameters: `include_edges`, `include_prose`, `edge_type_filter`.
5. **`explore`** — the general traversal primitive. Parameters:
   - `start` (node id; defaults to focus-stack top)
   - `depth` (default 1, max 3)
   - `include_types` / `exclude_types` (node and edge type filters)
   - `as_of` (event-time cutoff; future events excluded by default but **retrievable with `include_future: true`** for foreshadowing/direction)
   - `detail` (`names` | `summaries` | `full`)
   - `limit` (result cap, default conservative)
   - Scene assembly is an `explore` configuration: start at an Event, depth 1–2, include Characters/Locations/Items, detail = summaries.
6. **`find_path`** — shortest/meaningful paths between two nodes, filtered by allowed edge types, with a max-length bound. Used to surface non-obvious connections.
7. **`record_prose`** — create a `Prose` node from rendered output: text, referenced node ids, llm_input/llm_output, metadata. (A specialized wrapper over create_node kept separate because its payload shape and required references differ enough to merit its own contract.)

A possible eighth tool, **`refresh_summary`**, batch-fetches stale-summary nodes and writes regenerated summaries; alternatively this is folded into `update_node` + an `explore` filter (`only_stale: true`). Decide during implementation.

## Summary Lifecycle

- `summary` required at creation (forces the LLM to commit a one-liner at write time — cheap, and exploratory queries stay fast).
- Any `update_node` without a new summary flags `summary_stale`.
- v1 staleness is **self-only** (no propagation to neighbors). One-hop propagation along selected edge types is a noted future enhancement.
- Workflow: periodically (or before major rendering sessions) query for stale nodes, regenerate summaries via the LLM, write back.

## Character Knowledge Model (hybrid)

Continuity errors are usually characters knowing things they shouldn't. v1 approach:

1. **Derived knowledge:** a character is presumed to know about events they `participated_in` (and, transitively, facts those events established), filtered by `as_of` time.
2. **Background-implied knowledge:** characters' `description`/background fields imply general knowledge (a smith knows smithing); the rendering LLM applies judgment here rather than the graph enforcing it.
3. **Explicit `knows` edges** for exceptions and important secrets: information learned secondhand, or notably *unknown* despite participation. These override/extend the derived layer.

A future `knowledge_of(character, as_of)` query can assemble all three layers; for v1, `explore` with `as_of` + participation edges covers the common case.

## Phased Build Plan

**Phase 1 — Core writes & context.** Anytype API wrapper; schema setup; `create_node` (composite), `update_node` (with staleness flag), `get_node`; session state + `SessionContext` meta-node; context echo in all responses.

**Phase 2 — Retrieval.** `explore` with full parameter set (depth, type filters, detail levels, limits, `as_of`); `find_path`; focus-stack defaults wired into queries.

**Phase 3 — Story layer.** `Prose` node type + `record_prose`; stale-summary query/refresh workflow; consistency lookups (prose referencing a node).

**Phase 4 — Refinement & future-proofing.** Knowledge-model query helpers; staleness propagation experiment; type extensibility (`propose_type`); multi-user groundwork (per-user `SessionContext` nodes, project locking/merge considerations).

## Open Questions (deferred, noted for later)

- Multi-user conflict handling: last-write-wins vs. locking when two sessions edit one world.
- Whether `refresh_summary` is its own tool or a usage pattern.
- Embedding/semantic search over summaries as a complement to structural queries.
- Type extensibility mechanics (`propose_type` with human approval?).
