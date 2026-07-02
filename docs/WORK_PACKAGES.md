# Work Packages — graph-context-mcp

**Status:** WP0 (vertical slice) and **WP1 (Anytype adapter)** are complete: domain core, async port, both repository implementations, contract suite, sync engine, MockAnytype simulator (78 mock-backed tests) plus a live-gated E2E suite (`ANYTYPE_E2E=1`, 11 tests). The WP1 spike (S1–S8) was **run against a live local Anytype server on 2026-06-21** (API `2025-11-08`); answers and evidence are recorded inline under WP1.0 below. **Go/no-go: GO** — S1 (the load-bearing relation round-trip) passed. Most assumptions in `mapping.py` (A1–A4) held. The spike-driven **code corrections have since been applied** (resync via `POST /search`, timestamp-from-properties with `created_date` fallback, endpoint-split page caps, `plural_name` on type creation, faithful mock + the live-gated E2E suite) — see the "Spike results" note and the "applied corrections" list below. The adapter is verified against both the mock suite and a live server. This document specifies the remaining work in enough detail to pick up cold.

**How to read this:** "Decisions (settled)" are choices already made with rationale — do not relitigate without new information; write an ADR if you must. "Decisions (open, owner needed)" block or shape the WP and need a call at kickoff. "Open questions" can be answered during the work.

---

## Status addendum (2026-07-02)

**WP0–WP3 are complete** and green against both the mock suite and the live-gated E2E suite; the Definition of Done (tests, `ruff`, `mypy --strict`, demo scripts) holds. ADRs now exist in `docs/adr/` (001–005 backfill the decisions this document names; 006 is new — read it before this document's WP1/WP2 sections).

**The space-reflecting pivot (2026-06-27) supersedes this document's closed-vocabulary design.** WP1's "one Type per `NodeType`, one relation per `EdgeType`" bootstrap and the fixed `NodeType`/`EdgeType` vocabulary described below were implemented, dogfooded, and then replaced: the system now reflects the user's **native** Anytype types and relations (open vocabulary, live `SpaceRegistry`, key-derived edge labels, semantic `Role` layer; `gc_` keys survive only for infra — Prose, SessionContext, scalar properties, starter `gc_edge_*` relations). See **ADR 006** for the full decision. Sections below describing the closed schema are kept as history, not as spec.

Also beyond the original spec: a `find_node` tool (eighth tool), a derived `context action="overview"` cold-start map, and name-or-id resolution on every node parameter.

**WP4 remains parked** (entry criteria unchanged). The open frontier is now
WP5–WP7 — see the direction addendum below and ADRs 007/008.

---

## Direction addendum (2026-07-02) — beyond fiction, beyond MCP

The project's scope now extends past a fiction-only MCP server, in two
decisions made after dogfooding (full rationale in the ADRs; the summaries
here are pointers, not spec):

**ADR 007 — orchestrator as a second in-process interface adapter.** The
behaviors wanted next (automatic capture, provenance, mode-gated tool
availability) require seeing the conversation, which an MCP server never
does — it receives tool calls only. A new `orchestrator/` package (same
repo, LangGraph initially, quarantined like the Anytype quirks) imports the
application layer directly and reuses `interface/tools.py`; the MCP server
remains a supported standalone product. Modes: world-modeling (full
surface) vs authoring (read-only + focus; mutation tools not bound at all).

**ADR 008 — provenance is a harness responsibility.** The harness records
automatically what `record_prose` asked the model to volunteer: one
`gc_intent` node per mutating turn (verbatim prompt + condensed tool-call
trace in the write-once body; `intent` edges to every node touched,
populated at creation — one write per turn), chained to any captured
artifact. Hidden behind the infra-role mechanism; surfaced via
`get_node(include_provenance=N)`. **Supersedes WP3's
`llm_input`/`llm_output` parameters on `record_prose`** (kept below as
history); the tool itself survives as the voluntary path for harness-less
MCP clients.

WP5 (domain profiles — **shipped 2026-07-02**: `interface/profiles.py`,
`GC_PROFILE`, golden snapshot tests, workspace demo), WP6 (orchestrator
skeleton), WP7 (provenance & capture pipeline), and WP8 (multi-user: chat
transports, single-writer write scheduling, per-session state) below
specify the work.

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

### Spike results (2026-06-21, live server, API `2025-11-08`)

Run against a live local Anytype reached at `http://host.docker.internal:31009` (host's `localhost:31009`), bearer key from `/run/secrets/anytype_api_key`, in a throwaway `GC-Spike` space seeded to ~750 objects. Transcripts were produced with `httpx` (the full spike script is reproducible; a `docs/spike-anytype.md` write-up can be generated from these notes on request). **Gate verdict: GO.**

- **S1 — PASS (load-bearing).** A custom relation/"objects" property (`gc_edge_located_at`) was created, **populated at object creation**, and **modified via PATCH** — full round-trip confirmed (mapping **A1 holds**). PATCH **replaces** the multi-value list wholesale (sent `[Castle Brakk]`, read back exactly `[Castle Brakk]`, prior target gone) → **A4 holds; link updates require read-modify-write.** Write-payload shape is `{"key", <format-field>: value}` — the `"format"` field is *optional* on write (the server accepts and ignores it), so `mapping.py`'s entries (which include `format`) work as-is (**A3 essentially holds**). Note: Anytype auto-mirrors any relation into its built-in `links`/`backlinks` properties; `to_edges()` already ignores non-`gc_edge_*` keys, so no spurious edges result.
- **S2 — one-pass hydration, fast.** Properties are returned **inline** in both `GET /objects` lists and `POST /search` (mapping **A2 holds**) — no N+1. **Max page limit differs by endpoint:** `GET /objects` honors large limits with **no observed cap up to 1000** (554 objects returned in a single 71 ms call); `POST /search` is **hard-capped at 100/page** regardless of requested `limit`. Hydration of 555 objects took **71 ms in 1 call** (limit 1000) / 182 ms in 6 calls (limit 100); extrapolated, **2,000 objects hydrate in 2–3 `GET /objects` calls in well under 1 s** — crushes the <5 s budget and stays inside the burst. **Design consequence:** hydrate via `GET /objects` with a large page size; resync (which needs filters, see S3) must use `POST /search` and page at 100.
- **S3 — `last_modified_date` usable, with a wrinkle.** Granularity is **seconds** (`2026-06-21T22:27:55Z`). A **relation-only PATCH bumps `last_modified_date`** (verified) — so out-of-band relation edits are visible to resync. Sorting/filtering live **only on `POST /search`**, not on `GET /objects` (GET rejects filter params with HTTP 400). Filter shape: `"filters": {"property_key","condition","value"}` or `{"and":[…]}/{"or":[…]}`; `"sort": {"property_key","direction"}`; condition `greater_or_equal` works. **Wrinkle:** the `last_modified_date` *value* is **omitted from the property list for objects never modified since creation** (only `created_date` appears), yet the server still tracks, sorts, and filters by it internally (a `>= T` filter returned 308 objects; sort placed the genuinely-edited object first). **Implication:** don't derive the resync high-water-mark from returned object timestamps (they may be absent); track sync time client-side and query `last_modified_date >= T`, deduping the boundary second (use `>=`, it's idempotent).
- **S4 — deletions are INVISIBLE to incremental resync (confirmed risk).** `DELETE` = archive (soft delete; the DELETE response body's `archived` flag is stale/`false`, but a follow-up `GET` shows `archived: true`). Archived objects **disappear from both `GET /objects` and `POST /search`**, are **not** surfaced by a modified-since query, and **cannot be enumerated** (an `archived == true` filter is silently ignored — returns the full live set). They remain fetchable by id via `GET /objects/{id}`. **Consequence:** human deletions are undetectable incrementally; detecting them requires **full-set reconciliation** (hydrate all live ids, diff against the index). This drives **Q3** below.
- **S5 — custom types work.** `POST /types` with an explicit `key` (`gc_character`, `gc_location`) succeeds; the key is **honored, stable, and queryable**. Type-scoping is done via the top-level **`"types": [...]` array on `POST /search`**, not via `GET /objects` query params (GET rejects a `type=` param with 400). New types arrive with Anytype's default properties (`backlinks`, `tag`, `created_date`, `creator`, `links`) auto-attached, alongside our `gc_*` ones — harmless, the mapping ignores non-`gc_` keys.
- **S6 — body is write-once; generous size.** Body is supplied at creation as markdown and returned in the `markdown` field. **PATCH of `body` is silently ignored** (HTTP 200, but content unchanged) — confirms the historical limitation and **validates WP3's "render once, write-once body" design.** Bodies of **50 KB, 250 KB, and 1 MB all created and round-tripped** fully (1 MB in 480 ms); no practical size ceiling for Prose nodes. Minor caveat: markdown is normalized on store (trailing-whitespace/formatting), so byte-exact round-trip isn't guaranteed — store the rendered text, not a checksum-sensitive blob.
- **S7 — writes are the binding constraint, not reads.** **Reads are effectively unthrottled** (80 rapid `GET`s, 0×429). **Writes are throttled to ~1 req/s sustained** after a small burst (~30–60 before first 429 in this session). 429 body: `{"object":"error","status":429,"code":"rate_limit_exceeded","message":"You have reached maximum request limit."}` plus headers `ratelimit-limit/-remaining/-reset` and `x-rate-limit-*` (reset hint = 1 s). `ANYTYPE_API_DISABLE_RATE_LIMIT` is **not** set on this server. **Consequence:** bulk *creation* (seeding/import), not hydration, is the slow path — the client's exponential backoff on 429 is correct; per-tool single writes are fine, but any batch-create path must pace at ~1/s. (Seeding 2,000 objects for this very spike was the bottleneck — done partially, ~750, which was ample for the read measurements.)
- **S8 — auth is a two-step pairing; version header not enforced.** Error envelope is uniform: invalid key → `401 {"code":"unauthorized","message":"invalid api key"}`; missing header → `401 …"missing authorization header"`. Key lifecycle: `POST /v1/auth/challenges {app_name}` → `challenge_id`; user approves in the desktop app and reads a code; `POST /v1/auth/api_keys {challenge_id, code}` → bearer api_key. Keys are **per-app (named)** and **long-lived** (no expiry observed). **Surprise:** the `Anytype-Version` header is **not strictly enforced** — a bogus `1999-01-01` still returned 200. Version drift therefore won't surface as an error; keep pinning it but rely on the changelog, not runtime rejection, to catch breaking changes.

**Applied corrections (done — these resolved the spike findings in code):**
1. **Endpoint-split page caps.** `config.page_limit` → 1000 (the `GET /objects` hydrate sweep, which the endpoint honors); new `config.search_page_limit` = 100 (the `POST /search` cap). `mock_server.py` now mirrors this: `GET /objects` is uncapped, only `/search` caps at 100.
2. **Resync via search, not GET filters.** `client.search(types, filters, sort)` issues `POST /search` (body filters, query-param pagination); `sync.fetch_changes` uses `mapping.modified_since_filter()` + `mapping.ALL_TYPE_KEYS`. `GET /objects` is now the unfiltered hydrate sweep. The mock grew a faithful `/search` handler and `GET /objects` no longer accepts filters.
3. **Deletions via full reconciliation only.** The counterfactual `archived_visible_in_lists` knob (and its test) were removed; archived objects are invisible to list *and* search in the mock, matching live. The deletion path is the full-hydrate rebuild (`load_index`), exercised by both a mock test and the live E2E.
4. **Timestamps from properties, with `created_date` fallback.** `last_modified_date`/`created_date` are read as `date` properties via `mapping.effective_modified()` (last_modified else created), fixing the watermark/self-write-suppression against live (where `last_modified_date` is absent until first modification). The mock stamps these as date properties (created at creation, last_modified on change).
5. **`plural_name` on type creation.** `schema_bootstrap` now sends the API-required `plural_name` (naive `f"{value}s"` — cosmetic, human-editable, never clobbered by create-if-missing); the mock now 400s a type create that omits it, so the fake enforces the live contract.

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

- **Q1:** Should reads auto-resync on a staleness timer, or only on explicit request? Auto is friendlier; explicit is predictable and cheaper. **Spike S3/S7 say auto is cheap:** reads are unthrottled, the modified-since set is normally small, and each `POST /search` page is 100 — a resync is a handful of read calls. *Decision stands: default explicit-only, but auto-on-timer is now known-affordable; revisit after dogfooding.* Caveat: a timer-based resync still **cannot see deletions** (S4) — those need the periodic full reconciliation of Q3 regardless of the read-resync cadence.
- **Q2 (depends on S1):** **Resolved by S1: PATCH replaces the list wholesale, so link add/remove is read-modify-write.** v1 behavior is **last-write-wins**: read the current targets, apply the delta, PATCH the full list; if a human edited the same relation between our read and write, their change is silently overwritten — **log it loudly** (warn with node id + property + before/after) and document it. Optional hardening (Phase 4, not v1): re-GET and compare just before the PATCH to *detect* the race and warn precisely, still LWW.
- **Q3 (depends on S4):** **Resolved by S4: archived/deleted objects are invisible to both list and modified-since and cannot be enumerated** → modified-since resync will *never* report a deletion. Deletion detection requires **full-set reconciliation** (hydrate all live ids, diff against the index, drop the missing). Cadence: run it on **project open / full hydrate** (cheap per S2 — 2–3 calls for 2k nodes), not on every incremental resync. **Documented staleness window for deletions = time since last full hydrate** (i.e., a node deleted by hand mid-session stays in the index until the next open/explicit full reconcile). Surface this in the `context resync` notice copy so the user isn't surprised.
- **Q4:** `SessionContext` mirroring frequency (WP3 feature, but client/rate design should anticipate a debounced writer — don't build a client that assumes every write is user-initiated).

### Risks

Relations don't round-trip (S1) → fallback + escalate, weakens product premise. **[Spike: resolved — S1 passed, relations round-trip.]** Hydration N+1 and slow (S2) → budget: 2k nodes hydrated < 5s on localhost; if missed, add a persisted index snapshot (load snapshot, resync delta). **[Spike: comfortably met — properties are inline (no N+1) and `GET /objects` honors page size 1000, so 2k nodes ≈ 2–3 calls in <1s; the snapshot fallback is not needed at this scale.]** Worlds beyond ~5,000 nodes exceed the 60-call burst even in the happy path → that is the concrete trigger size for the snapshot fallback. **[Spike nuance: the burst budget binds *writes* (~1/s sustained), not hydration *reads*, which were unthrottled — so the binding constraint at scale is bulk import/create, and the snapshot fallback mainly helps cold-start reads; revisit the 5,000 trigger against read page-size 1000, not the 60-call write burst.]** Client design rule (implemented): `hydrate` is the only code path allowed to approach the burst budget; per-tool operations stay far below it, so a hydrate never starves an in-flight session.

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
- **Multi-user**: per-user `SessionContext`, conflict policy beyond LWW. Entry criterion: a second user exists. **[Superseded by WP8 (2026-07-02)** — the chat-transport direction turned the second user from hypothesis into plan; WP8 is the mini-spec this line asked for.**]**
- **Semantic search over summaries**: complement to structural queries; entry criterion: users ask "find the node about X" questions that name-search can't answer.

---

## WP5 — Domain profiles (generalize beyond fiction)

**Goal:** the same server serves a story world or a work knowledge base;
fiction becomes one *profile*, not the baked-in framing. ADR 006 already
made the schema open — what remains fiction-specific is prompt framing (the
tool docstrings), the `story_time` naming, and the Prose concept.

### Deliverables

- A `DomainProfile`: docstring framing fragments and worked examples, role-map
  overrides, capture-artifact framing (scene vs meeting note vs decision
  record), and time-axis framing ("story time" vs real timestamps — the
  mechanism and `gc_story_time` key are unchanged; only the words differ).
  Two shipped profiles: **`fiction`** (default; current docstrings verbatim)
  and **`workspace`**.
- Tool registration assembles docstrings from the active profile at startup.
  Docstrings are prompts — profile fragments are prompt engineering and get
  the same review bar; snapshot tests make each profile's assembled output a
  reviewable artifact.
- Prose generalizes to **capture** in concept and docs; the `gc_prose` type
  key stays for compatibility with existing spaces.
- README split: fiction quickstart vs workspace quickstart; a workspace demo
  script in `scripts/`.

### Decisions (settled)

- Fiction remains the default profile — existing setups see zero change.
- `gc_` keys (`gc_story_time`, `gc_prose`, …) are frozen for compat; profiles
  change framing, never storage keys.
- Profile selection via env (`GC_PROFILE`), consistent with `GC_BACKEND`.

### Open questions

- Should the profile eventually live per-space (persisted alongside
  SessionContext) rather than per-process env?
- How far docstring templating can go before two hand-written variants read
  better than one templated one — let the snapshot diffs decide.

### Tests

Golden/snapshot docstrings per profile; role-override behavior through the
registry; the workspace demo end-to-end against the fake.

---

## WP6 — Orchestrator skeleton (ADR 007)

**Goal:** a runnable agentic pipeline in this repo with two modes and
harness-owned tool binding, reusing the existing tool layer. No provenance
yet — that is WP7. Independent of WP5 (the skeleton can run fiction-framed).

### Deliverables

- `orchestrator/` package: LangGraph state machine with `world_modeling` and
  `authoring` mode nodes; per-mode tool binding over the `interface/tools.py`
  wrappers. In authoring mode the mutation tools are **not bound** —
  unavailable, not refused.
- Shared service builder: factor `_build_services` out of
  `interface/server.py` so both composition roots wire identically
  (config → client → bootstrap → repository → hydrate → session → services).
- Packaging: `[orchestrator]` optional extra in pyproject; langgraph added to
  the devcontainer image (egress firewall — container build, not ad-hoc pip).
- Import-linter contracts extended per ADR 007: orchestrator → interface
  (tools/presenters only, never `server.py`) → application → domain;
  orchestrator's composition root joins the infrastructure allowlist; no
  langgraph import outside `orchestrator/`.
- Transport-agnostic entry seam: `handle_message(session_id, user_id, text)
  → reply events`. The CLI chat loop is the first thin adapter over it
  (chat-bot transports arrive in WP8); presenter output stays
  transport-neutral.
- Demo script proving the acceptance scenario (switch modes; authoring mode
  cannot mutate).

### Decisions (settled)

- Same repo; in-process coupling; LangGraph quarantined so a framework swap
  is orchestrator-internal (all ADR 007).
- Mode switching is explicit (user command) for v1.
- **No MCP-wrapped pipeline** (settled 2026-07-02): the orchestrator is not
  re-exposed as an MCP server for Claude Desktop — two LLMs in the loop,
  and the outer model (not the harness) would still decide whether the
  pipeline runs. Claude Desktop keeps the plain tool server; the
  orchestrator's surface is its own (CLI first).

### Open questions

- Should `tools.py`/`presenters.py` move from `interface/` to a neutral
  shared package now that two adapters import them? (Defer until the second
  import actually lands; a rename-only PR is cheap.)
- Model-*suggested* mode switches with harness confirmation, once explicit
  switching has been dogfooded.

### Tests

Authoring-mode binding literally lacks mutation tools (asserted on the graph
definition, not on refusal behavior); the pipeline drives a scripted fake
LLM through both modes against the in-memory backend; import-linter enforces
the quarantine.

---

## WP7 — Provenance & capture pipeline (ADR 008)

**Goal:** provenance becomes automatic — intent nodes journaled per mutating
turn, authoring output captured with references, `record_prose`'s `llm_*`
parameters retired. Depends on WP6 (the harness must exist); pairs naturally
with WP5's capture generalization.

### Deliverables

- `MutationJournal` observer in the application layer: writers report
  created/modified node ids at the source (the writers already know — no
  parsing of presenter output). No-op journal in the MCP server; per-turn
  collector in the orchestrator.
- `IntentRecorder` application service: at end of a mutating turn, ONE
  `gc_intent` node — naming convention `Intent: <first ~60 chars> —
  <timestamp>`; write-once body = verbatim prompt + condensed tool-call
  trace, capped with `[truncated]`; `intent` edges to every touched node,
  populated at creation. Scalar properties `gc_user_id` (transport-scoped)
  and `gc_model` for attribution — in a shared space Anytype's own
  `creator`/`last_modified_by` show only the bot identity, so intent nodes
  are the real attribution record. Read-only turns write nothing.
- Authoring auto-capture: entity-link produced text against `GraphIndex`
  names (exact/alias matching for v1) → capture node with `references`
  edges; the intent node links to the artifact (prompt → intent → artifact
  + touched nodes).
- `get_node(include_provenance=N)` mirroring `include_prose`; edges to
  infra-role nodes suppressed in `get_node`/`explore` edge grouping by
  default (tool-layer policy — domain stays policy-free).
- Retire `llm_input`/`llm_output`/`model` from `record_prose` (public-surface
  change: run it through the WP2 param-naming review discipline).
- Config: subsystem on/off toggle; prompt-storage knob extending
  `GC_STORE_LLM_INPUT`.

### Decisions (settled)

- One intent node per user turn, not per mutation (write cost and coherence).
- Single `intent` edge label; created-vs-modified detail in the body.
- Fields-touched only in v1 — no before/after diffs.

### Open questions

- Multi-turn intent chains (`follows` edges) for "now continue"-style turns
  whose real intent lives earlier in the conversation.
- Whether captured text should *propose* graph updates (assertions absent
  from the graph → staleness flow) or merely flag referenced nodes.
- Retention of intent nodes: leave pruning to the human until dogfooding
  proves otherwise.
- Semantic entity-linking, tied to WP4's parked semantic-search item — whose
  entry criterion workspace usage makes far likelier to trigger.

### Tests

A mutating turn yields exactly one intent node with edges to every touched
node; a read-only turn yields none; body cap and truncation marker; hidden
from `explore` and edge groups by default; `include_provenance` ordering and
excerpts; the privacy knob suppresses prompt text. All through the contract
suite — intent nodes are ordinary nodes, so the fake needs no new
capability.

---

## WP8 — Multi-user (supersedes WP4's parked multi-user item)

**Goal:** several humans drive one orchestrator against one shared space —
chat transports as the surface, concurrent writes that never silently lose
a user's command, per-session state, attribution and privacy handled.
Depends on WP6 (harness + transport seam) and WP7 (intent nodes already
carry `gc_user_id`/`gc_model`).

### Deliverables

- **Chat-bot transports** behind WP6's `handle_message` seam: Telegram
  (long polling — outbound HTTPS only, least ops friction) and/or Slack
  (Socket Mode — outbound websocket; the natural surface for the workspace
  profile), Discord equivalent-shaped. Chosen per deployment. Message
  chunking and dialect shims (Slack mrkdwn, Discord 2k-char limit) live in
  the transport adapter; presenters stay transport-neutral. One message =
  one turn = at most one intent node. Transport egress joins the
  devcontainer firewall allowlist (or the bot runs outside the container).
- **Single-writer delta queue** (settled — see decisions). **Core shipped
  2026-07-02 (ADR 009):** FIFO single-writer seam in the adapter,
  store-truth PATCH materialization via fresh GET in the critical section,
  precise Q2 race detection, `pending_writes` depth surface, mock-transport
  yield fidelity, and the port-level concurrency contract test. Deferred to
  this WP: explicit pacing interval, fairness, and user-facing depth
  feedback. Original spec follows. All Anytype
  writes flow through one scheduler task inside the Anytype adapter, which
  also owns the ~1 write/s pacing. Queue entries are **deltas** ("add
  target T to relation R on node N", "set field F=V", "create node with
  links") — never precomputed relation lists. The current-targets read
  happens **at dequeue time, as a fresh GET** of the object (reads are
  unthrottled per S7), so every PATCH payload is materialized from the
  freshest state an instant before send. This closes the in-process
  read-modify-write race by construction (exactly one writer), narrows the
  human-vs-bot race (Q2) to the GET→PATCH gap and detects it precisely
  (the loud-log hardening Q2 deferred), and is the single place for
  queue-depth feedback ("queued behind N writes"). The **port contract**
  gains the guarantee: concurrent link mutations on one node all take
  effect. The fake meets it via synchronous atomic ops; the Anytype
  adapter via the queue. ADR 009 lands with the implementation PR.
- **Per-session state.** `SessionState` keyed by session id (transport
  thread/channel ↔ LangGraph `thread_id`): focus stack, recent list,
  project label per session. The `SessionStore` port extends to keyed
  load/flush (one `gc_session_context` node per session; debounce
  discipline unchanged); fake + contract tests move with it. The MCP
  server keeps its single implicit session — behavior unchanged.
- **Authorization at the bot layer.** Channel and user allowlists;
  per-user *mode* availability (WP6's mode binding extends per-user — e.g.
  authoring for everyone, world-modeling for editors). Config-driven for
  v1; unauthorized users' tools are unbound, not refused.
- **Privacy.** `GC_STORE_LLM_INPUT` evolves into per-user consent for
  prompt storage: intent nodes are visible to every space member, so an
  opted-out user's intent node keeps the tool-call trace but replaces the
  prompt text with `[prompt withheld by user preference]`. Default: store.
- **Shared-space operation documented** (README + docstrings): the bot
  identity owns the shared space; humans join via Anytype's space sharing
  (Editor/Viewer) and their devices sync continuously — there is no sync
  trigger to build. Auto-resync flips on (Q1 revisited): timer plus resync
  at turn start. The S4 deletion staleness window restated in multi-user
  terms. Chat-only users need no Anytype at all.

### Decisions (settled)

- **Single-writer delta queue over per-node locks.** A lock table solves
  only the race; the queue solves the race, the write pacing, and user
  feedback with one mechanism, and deltas-not-payloads makes stale-list
  clobbering unrepresentable rather than merely guarded.
- **Dequeue-time reads are fresh GETs, not index reads** — cheap
  (unthrottled) and it buys Q2's precise race detection for free.
- **Conflict policy stays LWW with loud logging for human-vs-bot**;
  bot-vs-bot lost writes are eliminated, not logged.
- Transport-scoped user ids (`discord:…`, `slack:U…`) are the identity for
  v1; no cross-transport identity mapping yet.

### Open questions

- Queue fairness: FIFO for v1; per-user round-robin if one user's bulk
  work starves others.
- Composite create-with-links rollback (archive on failed link write)
  interacting with queued deltas — compensating delta or inline rollback?
  Design at implementation.
- Should Viewer-role space members be allowed to mutate *through the bot*
  (which holds Editor rights)? Bot-layer authz must not silently outrank
  Anytype's own roles.
- Self-hosted `any-sync` network for privacy-sensitive workspace
  deployments.
- Cross-transport identity mapping, only if the same human on two
  transports becomes real.

### Tests

Contract: `asyncio.gather` of two `add_links` on one node → both edges
present (fake passes trivially; mock-backed adapter fails before the queue,
passes after). Scheduler: a delta enqueued against a node whose relation
list changed after enqueue still produces the correct final list; pacing
respects ~1 write/s with burst; queue depth is surfaced. Sessions: two
sessions mutate focus independently; restart restores both. AuthZ: an
unallowed user/channel reaches no tools (asserted on the binding, not on
refusal). Privacy: an opted-out user's intent node carries the trace but
not the prompt.

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

The direction-addendum work (ADRs 007/008):

```
WP5 domain profiles ─────────────┐
                                 ├──▶ WP7 provenance & capture ──▶ WP8 multi-user
WP6 orchestrator skeleton ───────┘
```

WP5 and WP6 are independent and parallelize; WP7 is strictly after WP6 (the
harness must exist) and lands best after WP5 (capture framing); WP8 follows
WP7 (it builds on intent attribution and the transport seam). Suggested
sizing: WP5 = M, WP6 = M, WP7 = L, WP8 = L. One severable piece: WP8's
single-writer delta queue is adapter-internal and useful before multi-user
(it also fixes pacing for bulk writes) — it may land any time after WP1.

## Risk register (top items)

| Risk | Signal | Mitigation |
|---|---|---|
| Relation properties don't round-trip (S1) | Spike | Fallback edge encodings + escalate (weakens "human-editable" premise) |
| Hydration too slow at scale (S2) | Spike timing | Persisted index snapshot + delta resync |
| Human deletions invisible to resync (S4) | Spike | Periodic full reconciliation; document staleness window |
| Tool surface churn after release | Param-naming review skipped | WP2's scheduled naming review before first external use |
| LLM misuses tools | Dogfooding transcripts | Docstrings-as-prompts discipline; iterate on descriptions, not new tools |
| Anytype API version drift | Changelog page | Pin `Anytype-Version`; subscribe to the changelog; bump deliberately |
| LangGraph abstractions leak into core layers | import-linter CI failure | ADR 007 quarantine; a framework swap must stay orchestrator-internal |
| Intent nodes clutter the human editing surface | dogfooding in the Anytype UI | Naming convention + infra-role hiding + subsystem toggle (ADR 008) |
| Bulk ingestion (workspace profile) hits the ~1 write/s throttle | import/seeding timing | Pace batch creates; design an explicit import path before promising one |
| Concurrent turns silently lose link writes (in-process RMW race) | WP8 contract test | Single-writer delta queue (WP8); until it lands, the orchestrator runs single-user |
| One user's prompts exposed to all space members via intent nodes | privacy review at WP8 | Per-user consent knob; `[prompt withheld]` marker keeps the trace usable |
