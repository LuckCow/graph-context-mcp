# Work Packages — graph-context-mcp

**Status:** WP0 (vertical slice) and **WP1 (Anytype adapter, mock-backed)** are complete: domain core, async port, both repository implementations, contract suite, sync engine, MockAnytype simulator, 79 tests. **No live Anytype server was available**, so WP1's spike questions are *encoded as assumptions* in `mapping.py` (A1-A4) and `mock_server.py` (knobs) rather than answered -- running the spike against a live server and correcting both files is now the first task for whoever has one. This document specifies the remaining work in enough detail to pick up cold.

**How to read this:** "Decisions (settled)" are choices already made with rationale — do not relitigate without new information; write an ADR if you must. "Decisions (open, owner needed)" block or shape the WP and need a call at kickoff. "Open questions" can be answered during the work.

---

## Cross-cutting: rules of the road

**Dependency rule.** `interface → application → domain`; infrastructure implements `ports`; only the composition root and tests import infrastructure. CI should eventually enforce this (import-linter), but code review enforces it now.

**Contract tests are the spine.** `InMemoryGraphRepository` is the executable spec. WP1's first task is to refactor the existing repository-touching tests into a shared contract suite (a base test class parameterized over implementations) that runs against the fake always and against live Anytype when `ANYTYPE_E2E=1`. Any behavior added to the Anytype adapter that the fake cannot express means the **port** is wrong — fix the port, not the adapter.

**ADRs.** Create `docs/adr/`. Backfill short ADRs for decisions already embodied in code: (001) Anytype as storage + human surface, (002) derived in-memory GraphIndex as the only traversal engine, (003) edges as relation properties, (004) summary-staleness rule lives in NodeWriter, (005) filters prune subtrees in traversal. New significant decisions get an ADR in the same PR.

**Definition of Done (every WP).** Tests green including contract suite; `ruff` and `mypy --strict` clean; README and tool docstrings updated; ADRs for new decisions; a runnable demo script in `scripts/` proving the WP's acceptance scenario.

**Logging.** Structured, per-operation, with durations. Never log prose content or summaries above DEBUG — this is a user's creative work.

### Decision required at WP1 kickoff: sync vs async (blocks WP1 and WP2)

The current port and services are synchronous. The MCP Python SDK is async, and the Anytype client will be `httpx`. Options:

1. **Convert port + services + repository to async now.** Mechanical change while the codebase is small (~10 signatures, tests get `pytest-asyncio`). The fake stays trivial (no awaits needed inside, just `async def`).
2. Keep the core sync and bridge in the tool layer via `anyio.to_thread`.

**Recommendation: option 1.** Option 2 leaves a seam that every future feature trips over (timeouts, cancellation, concurrent resync). Doing it later means touching every test. Whoever takes WP1 should land the async conversion as its first PR, before the spike results even arrive.

---

## WP1 — Anytype adapter (`infrastructure/anytype/`)

**Goal:** a production `AnytypeGraphRepository` that passes the contract suite against a live local Anytype, including hydrate and resync of out-of-band human edits.

### WP1.0 — Spike (timeboxed, do first, ~1–2 days)

Run against a live local Anytype, API version `2025-11-08`, base `http://localhost:31009/v1`. Output is a written report at `docs/spike-anytype.md` answering, with curl/httpx transcripts:

- **S1 (the load-bearing one):** Can a custom property of relation/"objects" format be created via the API, populated at object creation, and modified via PATCH? Does PATCH **replace** the multi-value list or merge it (i.e., do link updates require read-modify-write)?
- **S2:** Do list/search responses include properties inline, or only per-object GET? Decides whether hydration is one paged pass or N+1. Measure: time to fully hydrate a seeded space of ~2,000 objects. **Also: what is the maximum `limit` per page?** It sets hydrate's call count directly, and the documented rate limit (burst 60 requests, then 1 req/s sustained; `ANYTYPE_API_DISABLE_RATE_LIMIT=1` disables) makes call count, not latency, the binding constraint. N+1 at 2,000 nodes is ~33 minutes under the sustained rate -- disqualifying; one-pass is ~25-50 calls, inside the burst, ~0.5-3s.
- **S3:** Is `last_modified_date` returned, sortable, and usable to query "modified since T"? What granularity? Does it change when only a relation property changes?
- **S4:** Do **archived** objects appear in list/search results? Can archival be detected via modified-since queries? (Determines whether human deletions are visible to incremental resync at all.)
- **S5:** Custom **type** creation: are `type_key`s definable, stable, and queryable as search filters?
- **S6:** Body limits and PATCH-body behavior (known historical limitation: body patching unsupported/limited). Max practical body size for Prose nodes.
- **S7:** Rate limits and error payload shapes (see the Fundamentals → Rate Limits doc page); behavior on 429.
- **S8:** Anything surprising in auth/key lifecycle (key expiry, per-app keys).

**Go/no-go gate:** if S1 fails (relations don't round-trip), escalate before building. Fallback designs, in preference order: (a) edge-as-object — a `gc_edge` type whose name encodes `edge_type` and endpoint ids for searchability, properties carry the same data structurally; (b) per-node JSON adjacency blob in a text property. Both keep the port unchanged; both are uglier for human editing, which weakens the reason we chose Anytype — hence escalate, don't just pick.

### Deliverables

- `client.py` — thin async httpx client: bearer auth, `Anytype-Version` header, pagination iterator, bounded retry w/ backoff on 429/5xx, all failures wrapped in `AnytypeApiError(GraphContextError)` with status + endpoint. No domain knowledge.
- `schema_bootstrap.py` — idempotent: ensure node Types (one per `NodeType`), one relation Property per `EdgeType`, and scalar properties (`summary` text, `summary_stale` checkbox, `story_time` number, `description` text). Use a `gc_` key prefix to avoid colliding with user-created properties. Persist/discover the key→id mapping it creates.
- `mapping.py` — the quirk quarantine: `Node ⇄ Anytype object` translation, edge extraction from relation properties, archived-object filtering. **All** Anytype representation knowledge lives here and nowhere else.
- `repository.py` — `AnytypeGraphRepository` implementing the port. Write-through ordering: **persist to Anytype first, then update the index** (the index may lag the store, never lead it; a failed API call leaves the index untouched). Composite-create rollback = archive the node if any link write fails (matching the fake's tested contract).
- `sync.py` — `hydrate()` (full paged load on project open / rebuild), `resync()` (modified-since incremental; returns the set of changed node ids so the tool layer can surface "N nodes changed outside this session"), drift counters.
- `config.py` — pydantic-settings: API key, base URL, version, space id, page size, retry policy.
- Contract suite refactor (described above) + an E2E demo script: bootstrap an empty space, build the fixture world through the repository, kill the process, re-hydrate, assert graph equality; edit a node name in the Anytype UI by hand, `resync()`, assert the index reflects it.

### Tests

Contract suite against fake + live (gated). Adapter-only unit tests with `httpx.MockTransport` for: pagination stitching, retry/backoff, error translation, archived filtering, mapping round-trips (property→edge and back). Sync tests: modified-since picks up field edits; picks up (or documents inability to pick up, per S4) deletions; full-rebuild equivalence after random mutation sequences.

### Decisions (settled)

- Edges = relation properties on the **source** node, one property per `EdgeType`; reverse adjacency exists only in the index.
- Scalar fields (`summary`, `story_time`, …) are properties, never body. Body is reserved for Prose text (WP3).
- Anytype object ids are used verbatim as `NodeId`. Delete = archive.
- Concurrency with human edits is **last-write-wins** for v1; a mid-session human edit overwritten by the server is acceptable and documented. Locking/merge is Phase 4.
- Resync triggers: project open (full hydrate), explicit `context` resync action, and before `explore`/`find_path` if the last sync is older than a configurable threshold (default: off; see open question Q1).

### Open questions

- **Q1:** Should reads auto-resync on a staleness timer, or only on explicit request? Auto is friendlier; explicit is predictable and cheaper. Spike S3 timing data should inform this. Default to explicit-only; revisit after dogfooding.
- **Q2 (depends on S1):** If PATCH replaces multi-value lists, link writes are read-modify-write — define the retry/merge behavior when a human edited the same property between read and write (v1 answer is LWW, but log it loudly).
- **Q3 (depends on S4):** If archived objects are invisible to modified-since, pick a periodic full-reconciliation cadence (e.g., on project open only) and document the staleness window for deletions.
- **Q4:** `SessionContext` mirroring frequency (WP3 feature, but client/rate design should anticipate a debounced writer — don't build a client that assumes every write is user-initiated).

### Risks

Relations don't round-trip (S1) → fallback + escalate, weakens product premise. Hydration N+1 and slow (S2) → budget: 2k nodes hydrated < 5s on localhost; if missed, add a persisted index snapshot (load snapshot, resync delta). Worlds beyond ~5,000 nodes exceed the 60-call burst even in the happy path → that is the concrete trigger size for the snapshot fallback. Client design rule (implemented): `hydrate` is the only code path allowed to approach the burst budget; per-tool operations stay far below it, so a hydrate never starves an in-flight session.

---

## WP2 — MCP tool layer (`interface/`)

**Goal:** a running MCP server (stdio transport) exposing the v1 tool surface against either repository implementation, with the context echo on every response. Can be developed **in parallel with WP1** against the in-memory fake — only the composition root cares which repository it wires.

### Deliverables

- `application/node_reader.py` — the missing `get_node` use-case: full fields, edges grouped by edge type with neighbor names/summaries, `edge_type_filter`. (No `include_prose` yet — that parameter lands in WP3 so the surface doesn't ship a dead flag.)
- `interface/server.py` — composition root: FastMCP app, lifespan hook (load config → bootstrap schema → hydrate → construct session/services), manual constructor injection. One server process = one session = one active project (v1).
- `interface/tools.py` — tool definitions for: `context`, `create_node`, `update_node`, `get_node`, `explore`, `find_path`. Thin: validate params (pydantic) → call service → presenter. `record_prose` is WP3.
- `context` tool actions: get state; switch project (triggers hydrate); focus push/pin/unpin/remove/clear; `resync` (reports changed-node names); graph stats (node/edge counts, stale-summary count).
- Presenter expansion: `render_node` (grouped edges), `render_path` ("Mira —participated_in→ Siege of Brakk —located_at→ The Undercroft"), uniform error presenter (any `GraphContextError` → its message verbatim as a tool error; anything else → generic message + full server-side log).
- **Context header enforced centrally** — a single response-wrapping function every tool goes through, not per-tool discipline. A tool that forgets the header should be unrepresentable.
- `scripts/run_server.py` + a Claude Desktop / MCP-client config snippet in the README.

### Tool docstrings are prompts — treat them as such

The LLM chooses tools and parameters by reading these descriptions. Each must state: what the tool does in one line, parameter defaults and bounds, when to prefer it over neighbors, and one worked example. Required examples to include verbatim: scene assembly as an `explore` configuration (start at an Event, depth 1–2, `include_node_types=[Character, Location, Item]`, `detail=summaries`); foreshadowing via `as_of` + `include_future=true`; "create and link in one call" on `create_node`. Validation errors must echo the allowed values (e.g., bad node type lists the legal `NodeType` strings) — the consumer of every error is an LLM trying to self-correct.

### Decisions (settled)

- Responses are compact human-readable text (the presenter formats), **not** JSON — they are destined for a context window. Node ids always appear inline so follow-up calls can reference them.
- `explore` and `find_path` **exclude `Prose` and `SessionContext` node types by default**; they are infrastructure/derived content and would pollute scene assembly. Explicitly including them via `include_node_types` overrides this. (Implement as a default in the tool layer, not in domain traversal — the domain stays policy-free. Add a tool-layer test.)
- stdio transport first; HTTP later if remote use appears.
- Detail-level default for `explore` is `summaries` (proposal's conservative-defaults principle).

### Tests

In-process tool invocation against the fake (FastMCP supports direct call/test client): every tool's happy path; every documented validation error message contains the allowed values; the header is present on every response including error responses; project switch re-hydrates; resync notice renders changed names. Snapshot/golden tests of presenter output over the fixture world (these double as review artifacts when output formats change). Manual checklist before merge: run the server under a real MCP client and execute the scene-assembly example end-to-end.

### Open questions

- Surface the stale-summary count in the header itself (e.g., `| stale: 4`)? Cheap and useful; decide by trying it during dogfooding.
- Whether `context` should support `focus pop` distinctly from `remove` (proposal mentions pop; the stack API has `remove`) — pick one verb set and align tool + `FocusStack` naming.
- Parameter naming consistency pass before first external use: this is the public, hard-to-change surface. Schedule a 1-hour review with the team on names/defaults of all tool parameters.

---

## WP3 — Story layer

**Goal:** prose becomes part of the graph (recorded, referenced, queryable for consistency), the summary lifecycle gets its workflow, and session state survives restarts. Depends on WP1 (body writes, S6 limits) and WP2 (tool surface to extend).

### Deliverables

- `application/prose_recorder.py` + `record_prose` tool: creates a `Prose` node with `references` edges to every source node id supplied. Rendered text goes in the Anytype **body** (write-once — avoids the PATCH-body limitation). `llm_input`/`llm_output` stored as delimited sections in the same body, after the rendered text, capped at the size limit established in spike S6 with an explicit `[truncated]` marker. Generation metadata (model, timestamp) as properties.
- `get_node` gains `include_prose: int` (default 0): returns up to N most recent Prose nodes referencing this node, name + first ~M chars — the "how was this place described last time?" consistency lookup.
- Stale-summary workflow: `explore` gains `only_stale: bool` filter (tool layer narrows results to `summary_stale=True` nodes); `context` stats already count them (WP2). **Settled per the proposal's open question: no `refresh_summary` tool.** Rationale: it would be a composite of existing primitives (`explore only_stale` → LLM regenerates → `update_node` with fresh summaries); tool-surface minimalism wins, and the workflow is documented in the `explore`/`update_node` docstrings instead. Revisit only if dogfooding shows the LLM fails to execute the pattern reliably.
- `ports/session_store.py` + `infrastructure/anytype/session_repository.py`: `SessionState` serialized as JSON into a text property of a `SessionContext` meta-node. **Debounced** persistence: flush on project switch, server shutdown, and at most every N mutations (default 10) — never per-touch. Load on startup; corrupt/missing state degrades to a fresh session with a logged warning, never a crash.
- Demo script: render a scene (hand-written stand-in text), record it with references, restart the server, ask `get_node` on a referenced location and see the prose excerpt come back.

### Tests

Prose round-trip including a body at the S6 size limit and one over it (truncation marker present). `references` edges obey schema (Prose → any; nothing → Prose except via explicit include). `include_prose` ordering (most recent first) and excerpt bounds. Default exclusion of Prose from `explore` (already a WP2 test — extend with a real Prose node). Session persistence contract test (fake `SessionStore` + live-gated Anytype version): mutate focus → flush → reload → equal state; corrupted JSON → fresh session + warning.

### Open questions

- Excerpt length M for consistency lookups (start at 300 chars; tune by dogfooding).
- Should `record_prose` auto-create `references` edges to every node currently on the focus stack as a convenience default? Tempting; recommend **no** for v1 (explicit references keep provenance honest), note for Phase 4.
- Privacy/size of `llm_input`: storing full assembled prompts aids debugging but bloats the space. v1: store, capped; add a config flag to disable storing `llm_input` entirely.

---

## WP4 — Refinement (parked; entry criteria, not specs)

Take these up only after WP1–3 are dogfooded on a real story world. Each needs its own mini-spec when opened.

- **Knowledge query helper** (`knowledge_of(character, as_of)`): assemble participation-derived + background-implied + explicit `knows` layers. Entry criterion: the documented `explore` recipe demonstrably produces continuity errors in practice.
- **Staleness propagation** (one hop along selected edge types): entry criterion: stale-summary counts in dogfooding show self-only flagging misses real drift.
- **Type extensibility** (`propose_type`): entry criterion: the fixed vocabulary blocks a real story world; requires a human-approval flow design.
- **Multi-user**: per-user `SessionContext`, conflict policy beyond LWW. Entry criterion: a second user exists.
- **Semantic search over summaries**: complement to structural queries; entry criterion: users ask "find the node about X" questions that name-search can't answer.

---

## Sequencing

```
WP1.0 spike ──▶ WP1 adapter ──┐
       (async conversion PR    ├──▶ integration: WP2 server wired to WP1 repo ──▶ WP3
        lands first, unblocks  │
        both tracks)           │
WP2 tool layer (vs fake) ──────┘
```

WP1 and WP2 parallelize across two devs after the async-conversion PR merges; their integration point is one line in the composition root. WP3 is strictly after both. Suggested sizing: WP1.0 = S, WP1 = L, WP2 = M, WP3 = M.

## Risk register (top items)

| Risk | Signal | Mitigation |
|---|---|---|
| Relation properties don't round-trip (S1) | Spike | Fallback edge encodings + escalate (weakens "human-editable" premise) |
| Hydration too slow at scale (S2) | Spike timing | Persisted index snapshot + delta resync |
| Human deletions invisible to resync (S4) | Spike | Periodic full reconciliation; document staleness window |
| Tool surface churn after release | Param-naming review skipped | WP2's scheduled naming review before first external use |
| LLM misuses tools | Dogfooding transcripts | Docstrings-as-prompts discipline; iterate on descriptions, not new tools |
| Anytype API version drift | Changelog page | Pin `Anytype-Version`; subscribe to the changelog; bump deliberately |
