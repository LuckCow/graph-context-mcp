# graph-context-mcp

An MCP server exposing a knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph is the source of truth; the LLM builds it and writes from it. The framing is selectable ([domain profiles](#domain-profiles-gc_profile)): a **story world** (characters, locations, events, rendered prose — the original and default surface), a **work knowledge base** (people, teams, projects, meetings, decisions), or a **personal assistant** (tasks, procedures, notes). See `docs/` (proposal) for the full design.

This repository contains, from the storage core up: an async `GraphRepository` port with two certified implementations (in-memory fake and `AnytypeGraphRepository`), a contract test suite that runs against both, a sync engine (hydrate + incremental resync with self-write suppression), `MockAnytype` (an in-process simulator of the documented local API), a running FastMCP stdio server exposing the eight tools, body-backed node descriptions ([ADR 010](docs/adr/010-descriptions-in-the-body.md)), write-once-by-policy capture bodies, and debounced `SessionContext` persistence behind a `SessionStore` port. Above it: an **orchestrator harness** (WP6 — mode-bound tools, a real Claude driver on your subscription), **automatic provenance** (WP7 — one intent node per mutating turn, auto-capture in authoring modes), **Discord and Anytype in-space chat transports** (WP8/WP14 — the bot chats inside your Anytype spaces and deep-links the objects it creates, [ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)), **semantic search with graph-aware ranking** ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)/[016](docs/adr/016-graph-aware-ranking.md), WP11), and **configurable activity modes** ([ADR 015](docs/adr/015-configurable-activity-modes.md), WP12).

**Space-reflecting (v2, [ADR 006](docs/adr/006-space-reflecting-open-schema.md)):** the server reflects your *existing* Anytype space — native types (`character`, `event`, …) are nodes and every `objects`-format relation (yours or bootstrapped) is a labelled edge. There is no closed `gc_` vocabulary anymore; `gc_` keys survive only for infrastructure (Prose, SessionContext, and the stale-flag/story-time/fields scalars — summaries live in the built-in `description` property, [ADR 011](docs/adr/011-summary-in-builtin-description.md), and long-form descriptions in the body, [ADR 010](docs/adr/010-descriptions-in-the-body.md)). Architecture decisions live in [`docs/adr/`](docs/adr/).

**Live-server status:** the WP1 spike was run against a real local Anytype (API `2025-11-08`, 2026-06-21) and the assumption-driven corrections are applied in `infrastructure/anytype/` (resync via `POST /search`, endpoint-split page caps, timestamps-from-properties, `plural_name` on type creation). Node descriptions live in the object **body** ([ADR 010](docs/adr/010-descriptions-in-the-body.md)): created via `body`, updated via the `markdown` PATCH key (A7 — the S6 "write-once" finding was corrected 2026-07-02), fetched on demand and never hydrated. The mapping assumptions A1–A8 in `mapping.py` are mirrored by `mock_server.py`; a live-gated E2E suite (`ANYTYPE_E2E=1`) runs the same contracts against a real server.

Try it: `PYTHONPATH=src python scripts/demo_wp2_tools.py` — drives the full tool loop in-process (composite create → scene-assembly `explore` → `find_path` → stale-summary sweep → resync reporting → actionable errors) against the mock-backed repository.

```
pip install -e ".[dev]"
pytest          # mock-backed suite + live-gated E2E (ANYTYPE_E2E=1); live server not required
ruff check src tests
```

## Domain profiles (GC_PROFILE)

The schema is space-reflecting and domain-neutral (ADR 006); what a profile changes is **framing and behavior configuration** (ADR 015): the tool docstrings the LLM reads (they are prompts), native type-key → role mappings, the orchestrator's **activity modes** (each a goal prompt + tool binding + capture policy), and the **timeline source**. Tool names and parameters are identical across profiles.

| | `fiction` (default) | `workspace` | `assistant` |
|---|---|---|---|
| Framing | characters, scenes, foreshadowing | people, projects, meetings, decisions | tasks, procedures, notes |
| Activity modes | `world_modeling`, `authoring` (captures prose) | `world_modeling`, `authoring` (captures drafts) | `organizing`, `record_procedure` (captures native `procedure` nodes), `meeting_notes` (captures `note` nodes) |
| Timeline | `gc_story_time` (a story number) | `gc_story_time` (epoch/YYYYMMDD) | `event_date` (real ISO dates; `as_of="2026-07-04"`) |

Select with `GC_PROFILE=<name>` (unset = `fiction`; existing setups see zero change). Deployments can add or override activity modes via a `GC_MODES_FILE` TOML file — *Record Procedure is a configuration entry, not a fork*. Try the surfaces in-process:

```
PYTHONPATH=src python scripts/demo_workspace_profile.py
PYTHONPATH=src python scripts/demo_wp12_assistant.py   # record_procedure end-to-end
```

Profile docstrings are pinned by golden snapshot tests (`tests/interface/golden/`) — editing them is prompt engineering, and the golden diff is the review artifact (`GC_REGEN_GOLDENS=1 pytest tests/interface/test_profiles.py` to regenerate deliberately).

### Editing modes inside Anytype

Modes are also configurable **in the space itself** (ADR 015 amendment): every object of the bootstrap-minted **Activity Mode** type defines one mode. The first run seeds an *Example Mode* template whose body walks through the fields; the short version:

- The object **name** becomes the `/mode` name ("Faithful Scribe" → `/mode faithful_scribe`); naming it after a built-in (e.g. `world_modeling`) overrides that mode.
- The **page body** is the goal prompt the model follows — e.g. *"Record only what the user explicitly states; organize and link it, but never invent or embellish details."*
- Tick **`gc_mode_mutating`** to allow graph edits; fill **`gc_capture_type`** (plus optional `gc_capture_references`, `gc_capture_min_chars`) to auto-capture substantial replies; **archive** the object to disable the mode.
- Edits apply when `/mode` is next used in chat (any transport, Discord included) — `/mode` reloads and lists, `/mode <name>` switches. No restart. Precedence: profile defaults < `GC_MODES_FILE` < in-space.

## Running the MCP server

The server speaks **stdio** (one process per client; no network port). Run it directly only for a quick local check:

```
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server   # dev: in-memory, nothing persists
```

## Running the orchestrator (CLI / Discord / Anytype chat)

The orchestrator is the agentic harness over the same tool surface (WP6): a driver decides, activity modes bind tools, provenance records each mutating turn. Every transport shares one runtime assembly (`orchestrator/bootstrap.py`) and differs only in its message loop.

```
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.orchestrator.cli   # keyboard loop; dev backend
python -m graph_context.orchestrator.discord_bot                           # Discord bot (WP8), live backend by default
python -m graph_context.orchestrator.anytype_chat_bot                      # Anytype in-space chat bot (WP14/ADR 019)
```

**Anytype chat (the all-in path, [ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)):** the bot chats *inside* your Anytype spaces — the same store that holds the graph — and rewrites object references in its replies into clickable `anytype://` deep links. Served spaces are declared in `spaces.toml` (`GC_SPACES_FILE`), keyed by the space id, with optional `profile` / `project` / `modes_file` / `chat_id` (unset = the space's only chat is discovered; several chats = pick one, loudly). Identity follows the `<transport>:<id>` convention (`anytype:<chat_id>` sessions, `anytype:<member_id>` users) and each intent node's `origin` field points at the exact chat message that caused it. The chat cursor persists (`GC_CHAT_CURSOR`, default `logs/chat_cursor.json`), so **messages sent while the bot was down are answered on the next startup** (up to the API's ~100-message window); a chat bound for the first time skips its history instead. Never bind one space in both `spaces.toml` and `channels.toml`. Today the bot talks to the desktop app's API and posts as your own account; the prepared headless sidecar (bot account, own sync, no rate limit — `docker compose --profile sidecar`) takes over at cutover with just an `ANYTYPE_API_BASE_URL` flip (see WORK_PACKAGES WP14).

The Discord bot reads its token from `DISCORD_BOT_TOKEN_FILE` and serves **only** the channels you configure — both are wired in `.devcontainer/docker-compose.yml` (token secret at `/run/secrets/discord_bot_token`; no channel config = serve nowhere, loudly). Two configuration shapes ([ADR 017](docs/adr/017-channel-bound-spaces.md)):

- **`GC_DISCORD_CHANNELS`** (legacy allowlist): every listed channel shares the one env-configured runtime (`ANYTYPE_SPACE_ID`, `GC_PROFILE`), and a shared turn lock serializes their messages.
- **`GC_CHANNELS_FILE`** (channel-bound spaces): a TOML file mapping each channel to its **own Anytype space**, with optional per-channel `profile`, `project` label, and `modes_file`; each channel gets a fully independent runtime (own graph, focus/recent session persisted in its own space, provenance journal), and only same-channel turns serialize. One channel per space — a space holds a single SessionContext node. Setting both variables is ambiguous and fails at startup.

```toml
[channels.1523551542123298896]
space_id   = "bafyre..."        # required
profile    = "fiction"          # optional; defaults to GC_PROFILE
project    = "Ashfall"          # optional cosmetic label
modes_file = "ashfall-modes.toml"  # optional; overrides GC_MODES_FILE for this channel
```

It connects outbound via the Gateway websocket, so it runs inside the firewalled devcontainer (egress rules in `.devcontainer/init-firewall.sh`); the **Message Content** privileged intent must be enabled in the Discord developer portal or every message arrives empty. `GC_DRIVER=claude` (default) talks to the model on your Claude subscription (`GC_DRIVER_MODEL` / `GC_DRIVER_EFFORT` tune it); `GC_DRIVER=manual` is the keyboard stand-in (`/tool <name> {json}`) and works over Discord too. The **mode** is per-channel (`/mode <name>` switches it for that channel). Provenance is on by default (`GC_PROVENANCE=0` disables; `GC_STORE_LLM_INPUT=0` withholds prompt text from intent nodes). Every turn is also written in full — the user's message, each model decision, every tool call with its complete output, and the final replies, each entry stamped with the active mode — to a size-capped JSONL diary: `GC_TURN_LOG` sets the path (default `logs/turns.jsonl`; `0` disables), `GC_TURN_LOG_MAX_BYTES` the cap (default ~10 MB; the oldest entries are dropped once exceeded).

## Connecting Claude Desktop (from the dev container)

Claude Desktop runs on your **host**; the server runs **inside the dev container**. The host can't launch the container's Python directly, so Claude Desktop starts the server *inside the already-running container* over stdio with `docker exec -i`. (This is also how VS Code attaches — the container's compose `environment:` is inherited by every `docker exec` session, so the in-container env vars below are already set; the `-e` flags just pin the per-launch ones.)

**1. Start the container** (it must be running before Claude Desktop launches the server):

```
docker compose -f .devcontainer/docker-compose.yml up -d --build
```

**2. Add the server to Claude Desktop's config.** Copy the `graph-context` entry from [`.devcontainer/claude_desktop_config.example.json`](.devcontainer/claude_desktop_config.example.json) into your host's `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "graph-context": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "GC_BACKEND=memory",
        "graph-context-mcp-dev",
        "python", "-m", "graph_context.interface.server"
      ]
    }
  }
}
```

**3. Restart Claude Desktop.** It spawns `docker exec -i … python -m graph_context.interface.server`, which it speaks JSON-RPC to over stdio. You should see the eight tools (the 🔌 / tools menu). The first smoke test uses `GC_BACKEND=memory` — no Anytype, nothing persists — so it isolates the transport from storage. `docker` must be on Claude Desktop's `PATH` (Docker Desktop puts it there; if not, use the full path to the `docker` binary as `command`).

### Graduating to the live Anytype backend

`GC_BACKEND=anytype` (the default) talks to the Anytype desktop app on your host via `host.docker.internal:31009`. The container already wires everything it needs in `docker-compose.yml`: the base URL (`ANYTYPE_API_BASE_URL`), the file-mounted key (`ANYTYPE_API_KEY_FILE`), and the default space (`ANYTYPE_SPACE_ID`, pointing at the **TestWorld** space). The only thing you must create is the key file — `.devcontainer/secrets/anytype_api_key` (see [`.devcontainer/secrets/README.md`](.devcontainer/secrets/README.md)).

Because all three are container defaults inherited by `docker exec` — and `PYTHONPATH` is baked into the image (`ENV` in the Dockerfile) so the package imports in every `docker exec` session — the live backend needs **no `-e` flags at all** — `anytype` is the default backend. Drop the `memory` override and the entry becomes:

```json
{
  "mcpServers": {
    "graph-context": {
      "command": "docker",
      "args": [
        "exec", "-i", "graph-context-mcp-dev",
        "python", "-m", "graph_context.interface.server"
      ]
    }
  }
}
```

To point at a different space, add `-e ANYTYPE_SPACE_ID=…` to the `args`. (`config.py` reads the key from `ANYTYPE_API_KEY_FILE`, falling back to an inline `ANYTYPE_API_KEY`, and accepts either `ANYTYPE_API_BASE_URL` or `ANYTYPE_BASE_URL`.) Set `GC_STORE_LLM_INPUT=0` to withhold prompt text from the orchestrator's provenance (intent) nodes — the tool-call trace is kept either way.

Tools exposed: `context`, `create_node`, `update_node`, `get_node`, `explore`, `find_path`, `find_node`, `query`. (Prose/artifact capture is the orchestrator harness's job — WP7 auto-capture; there is no capture tool.) Every node parameter accepts a node **name** as well as an id (ambiguous names report their candidates); `find_node` covers browsing and disambiguation. Validation errors echo the allowed values (they are written for an LLM to self-correct). (Responses used to be prefixed with a `[project | focus | recent]` context header; it was removed 2026-07-06 as token waste — the session still tracks focus and recent nodes.) Tool docstrings are prompts — see `interface/server.py`. **Cold start:** a fresh session has an empty focus stack, so traversal has nothing to default to; `context action="overview"` (alias `map`) returns a *derived* entry-point map — per-type counts plus the highest-degree "hub" nodes with their ids — to seed the first `explore`/`get_node`/`focus`. It is rebuilt from the graph each call (no maintained root node).

## Semantic search (GC_EMBEDDER)

"Find the node I'm describing" — as a **derived projection**, never a new source of truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)). `GC_EMBEDDER` selects the embedder: `off` (default — byte-identical to pre-WP11 behavior), `hash` (deterministic bag-of-words, used by the offline test suite), or `local` (sentence-transformers over the image-baked `BAAI/bge-small-en-v1.5`; `GC_EMBEDDER_MODEL` overrides). Embeddings live in a SQLite cache (`GC_SEMANTIC_CACHE`, default `~/.cache/graph-context`) keyed by `(node_id, content_hash, model)` — deleting the file always converges on the next hydrate; it is disposable by design.

When enabled, `find_node` gains a third tier (exact → substring → semantic, hits labelled so the LLM knows it holds a fuzzy match) and `NodeNotFound` errors append "closest by meaning" candidates with ids and evidence. Retrieval runs through the **Ranker** ([ADR 016](docs/adr/016-graph-aware-ranking.md)): semantic recall seeds, graph expansion recruits (edges are relevance evidence), spreading activation scores, and every hit carries a nameable evidence line ("linked to Mira (possesses, strong match)"). **Non-feature by decision:** semantic never silently resolves a mutation target — exact resolves, semantic *suggests*. Try it: `PYTHONPATH=src python scripts/demo_wp11_search.py`; the ranking eval golden lives at `tests/semantic/ranking_eval.toml`.

## Architecture in one paragraph

Anytype is durable storage and the *human editing surface* — but its API only text-searches names/snippets, so all graph traversal happens in an in-memory `GraphIndex`: a **derived, rebuildable projection** that repository adapters keep coherent (write-through on our writes, hydrate/resync for edits humans make directly in the Anytype UI). Everything above storage follows a strict dependency rule:

```
interface  ──▶  application  ──▶  domain
   (MCP tools,      (use-cases,       (pure logic:
    presenters)      one per tool)     graph, traversal,
                          │            schema, session)
                          ▼
                       ports  ◀──implemented by──  infrastructure
                  (GraphRepository)                 (in-memory fake +
                                                     Anytype adapter)
```

**The rule:** imports only point left-to-right along the arrows. Domain imports nothing but itself and `errors`. Application imports domain + ports. Infrastructure implements ports. Nothing imports infrastructure except the composition roots — `interface/server.py` and the orchestrator's (`cli.py`, `bootstrap.py`, `discord_bot.py`, `anytype_chat_bot.py`), both delegating to the shared service builder `composition.py` — and tests. The orchestrator is a **second interface adapter** ([ADR 007](docs/adr/007-orchestrator-second-interface-adapter.md)): it reuses `interface/tools.py` but never the MCP module, and agent/transport frameworks (claude-agent-sdk, langgraph, discord.py) never leak outside it. All of this is machine-enforced: eight import-linter contracts in `pyproject.toml` fail CI on violation.

## Layout

| Path | Role | Key idea |
|---|---|---|
| `domain/schema.py` | Open type vocabulary + semantic `Role` layer | Types/edges are whatever the space has ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)); an editable type-key→Role map drives timeline/`as_of` and infra-hiding |
| `domain/overview.py` | Derived cold-start map | Per-type counts + highest-degree hubs; rebuilt per call, nothing maintained |
| `domain/models.py` | `Node`, `Edge`, `NodeDraft`, `LinkSpec` | Immutable; ids minted by storage, hence draft vs node |
| `domain/graph.py` | `GraphIndex` adjacency projection | The traversal engine's substrate; rebuildable, never authoritative |
| `domain/traversal.py` | Bounded BFS (`explore`) | Pure function; filters prune subtrees; `as_of` hides future events |
| `domain/pathfinding.py` | Bounded shortest path (`find_path`) | Undirected walk, direction-preserving result |
| `domain/session.py` | `FocusStack`, `RecentHistory`, `SessionState` | Working *set* not a pointer; pinning; top never evicted |
| `ports/graph_repository.py` | Persistence contract | Composite-create **rollback contract**; `fetch_body` for on-demand descriptions/prose |
| `ports/session_store.py` | Session-snapshot contract | Plain-dict snapshots; lenient load (corrupt → `None`) |
| `ports/mode_store.py` | Activity-Mode config contract | Plain payload dicts; validation lives in the loader, not the store |
| `ports/semantic.py` | `Embedder` + `SemanticIndex` contracts | Embeddings are a cache keyed by content hash + model, never truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)) |
| `application/node_writer.py` | `create_node` / `update_node` use-case | Owns the summary-staleness rule; touches focus |
| `application/node_reader.py` | `get_node` use-case | Grouped edges + WP7 `include_provenance` excerpts |
| `application/explorer.py` | `explore` / `find_path` use-case | Resolves focus-stack defaults |
| `application/capture_recorder.py` | Capture service (orchestrator-called) | Policy-typed artifacts ([ADR 015](docs/adr/015-configurable-activity-modes.md)); native types are first-class, only `gc_prose` keeps infra hiding |
| `application/mutation_journal.py` | Writers report created/modified ids at the source | `NullJournal` in the MCP server; drained per turn in the orchestrator |
| `application/intent_recorder.py` | One `gc_intent` node per mutating turn | Provenance is a harness responsibility ([ADR 008](docs/adr/008-provenance-as-harness-responsibility.md)) |
| `application/semantic_projector.py` | The embedding cache tracks the graph | Full pass + prune after hydrate; incremental from resync; store touches never re-embed |
| `application/ranker.py` | Graph-aware retrieval ([ADR 016](docs/adr/016-graph-aware-ranking.md)) | Recall seeds → graph recruits → activation scores; every hit carries evidence |
| `application/session_persister.py` | Debounced session persistence | Flush every N / on shutdown; lenient `load_or_fresh` |
| `composition.py` | Shared service builder | One wiring; both composition roots delegate to it |
| `infrastructure/memory/` | `InMemoryGraphRepository` + `InMemorySessionStore` + `InMemoryModeStore` | Reference impls; certified by `tests/contract` |
| `infrastructure/semantic/` | Hash + sentence-transformers embedders; memory + SQLite index | `GC_EMBEDDER` selects; the SQLite cache file is disposable |
| `infrastructure/anytype/client.py` | Async httpx client | Auth, version pin, pagination, bounded retry; `request_count` for budget asserts |
| `infrastructure/anytype/mapping.py` | The quirk quarantine | All representation assumptions (A1–A8) live here |
| `infrastructure/anytype/registry.py` | `SpaceRegistry`: the space's live types/relations | Resolves requested types & relation labels to existing keys; unknown labels surface for approval |
| `infrastructure/anytype/schema_bootstrap.py` | Idempotent **infra-only** bootstrap | gc_ infra types (Prose, SessionContext, Activity Mode + its example object), scalar gc_ properties, starter `gc_edge_*` relations — story entities use the space's native types |
| `infrastructure/anytype/sync.py` | Hydrate / resync engine | Lenient reads, strict writes; search-based modified-since |
| `infrastructure/anytype/repository.py` | `AnytypeGraphRepository` | Persist-first write-through; composite rollback; self-write suppression |
| `infrastructure/anytype/session_repository.py` | `AnytypeSessionStore` | Snapshot JSON in a `SessionContext` meta-node's property |
| `infrastructure/anytype/mode_store.py` | `AnytypeModeStore` | One mode per `gc_activity_mode` object: name → `/mode` slug, page body → goal, archive = disable |
| `infrastructure/anytype/chat.py` | Chat quirk quarantine + `AnytypeChatClient` | Chat payload/SSE assumptions (C1–C6, spike S10); the chat analogue of `mapping.py` |
| `infrastructure/anytype/mock_server.py` | `MockAnytype` | Spike-pinned behavior simulator (search caps, body-editing quirks, timestamps, chat routes + live SSE) |
| `interface/presenters.py` | Detail levels + node/path views | Response-budget shaping lives at the edge, not in tested logic |
| `interface/tools.py` | The eight tools (SDK-free) | `guarded` wrapper: actionable errors + per-call logging |
| `interface/profiles.py` | Domain profiles + `ModeSpec` defaults | Docstrings are prompts; golden-pinned per profile |
| `interface/server.py` | MCP composition root | Only module importing the MCP SDK; lifespan wiring |
| `orchestrator/pipeline.py` | `handle_message` turn loop | Per-turn tool budget; drains the journal into an intent node at turn end |
| `orchestrator/modes.py` | `ModeSpec` loader (profile < `GC_MODES_FILE` < in-space) | Unbound tools don't exist in the session — unavailable, not refused; `/mode` re-reads all sources |
| `orchestrator/drivers.py` | `LLMDriver` seam + scripted/manual drivers | Transcript + tool docs + mode goal in; tool calls or a reply out |
| `orchestrator/claude_driver.py` | The real model behind the seam | claude-agent-sdk on your Claude subscription; the SDK never executes tools — calls are harvested and returned as the decision |
| `orchestrator/capture.py` | Authoring auto-capture | Exact-name entity linking; the harness records what tools used to ask for |
| `orchestrator/turn_log.py` | Full-fidelity turn diary (JSONL) | Input, every driver decision, every tool call + complete output, final replies; byte-capped — oldest entries drop |
| `orchestrator/bootstrap.py` | Orchestrator runtime wiring | Shared by every transport; `GC_DRIVER` / `GC_PROVENANCE` / `GC_TURN_LOG` resolution; one runtime per channel (ADR 017) or space (ADR 019) binding |
| `orchestrator/channels.py` | Channel→space bindings (`GC_CHANNELS_FILE`, ADR 017) | Plain parsing/validation; one channel per space, enforced at startup |
| `orchestrator/discord_transport.py` + `discord_bot.py` | Discord adapter (WP8) | Per-message policy is plain logic; only the composition-root shim imports discord.py |
| `orchestrator/spaces.py` | Space→chat bindings (`GC_SPACES_FILE`, ADR 019) | Table key IS the space id; `chat_id` optional (single-chat discovery) |
| `orchestrator/rendering.py` | Shared reply rendering | `render` prefixes + `chunk`ing, extracted from the Discord module |
| `orchestrator/anytype_chat_transport.py` + `anytype_chat_bot.py` | Anytype in-space chat adapter (WP14) | Echo suppression, persisted chat cursor (offline catch-up), `anytype://` deep links; only the composition root touches infrastructure |

## Conventions to carry forward

1. **Business rules live in exactly one place.** Edge legality → `GraphIndex.add_edge`. Creation invariants → `schema.validate_new_node`. Staleness → `NodeWriter`. If you find yourself re-checking a rule in a second layer, the rule is in the wrong place.
2. **Domain stays pure.** No I/O, no `httpx`, no MCP types, no clocks. This is why `tests/unit` runs in milliseconds and can test traversal semantics exhaustively.
3. **Services take dependencies via constructor**, typed against ports. New tool ≈ new service module with the same shape as `Explorer`.
4. **Parameter objects mirror the tool surface.** `ExploreQuery` is the `explore` tool's schema in domain form; keep them in lockstep.
5. **Errors derive from `GraphContextError`** and carry actionable messages — the consumer of every error string is an LLM deciding what to do next.
6. **Fakes are contracts.** A behavior added to the Anytype adapter must land in `InMemoryGraphRepository` too, with a test. If the fake can't express it, the port is wrong — fix the port.
7. **Test names state behavior** (`test_failed_link_rolls_back_the_created_node`), grouped in classes per scenario; fixtures build worlds through public APIs, never by poking internals.

## Status & what's next

**Shipped** (full history + specs in `docs/WORK_PACKAGES.md`): WP0–WP3 (storage core, seven-tool MCP server, capture bodies, session persistence), the space-reflecting pivot ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)), WP5 (domain profiles), WP6 (orchestrator harness incl. the real Claude driver, 2026-07-06), WP7 (automatic provenance + auto-capture), WP9 (descriptions in the body), WP10 (attribute reflection, summary in the built-in description, connections footer), WP11 stage 1 (semantic search + graph-aware ranking, incl. the local embedder), and WP12 (configurable activity modes + the `assistant` profile). WP8 is **partially shipped**: the single-writer delta queue core ([ADR 009](docs/adr/009-single-writer-delta-queue.md)), the Discord transport, and channel-bound spaces ([ADR 017](docs/adr/017-channel-bound-spaces.md) — one Discord channel per Anytype space, with per-channel profile, session, and modes) are live. Definition of Done holds: `pytest`, `ruff`, `mypy --strict`, and `lint-imports` are all clean; CI runs exactly these on every push.

**Open work, in rough order of proximity:**

- **WP8 remainder (multi-user):** per-user `SessionState` *within* one space (channels bound to different spaces already have independent sessions; same-channel turns still serialize behind that route's lock), per-user mode authorization, per-user prompt-storage consent, Telegram/Slack transports, queue pacing/fairness/user-facing depth feedback.
- **WP11 deferred items (dogfooding-gated):** passage-level stage 2 + the reserved `search` tool, reranker adapters, the Voyage embedder (needs a firewall allowlist entry + key), the `GC_EMBEDDER` `off`→`local` default flip, orchestrator RAG prefetch (the Ranker's `session_seeds` parameter is ready for it).
- **Cross-turn driver memory:** each `decide()` is deliberately a fresh stateless session; the SDK's session-resume machinery is the lever when dogfooding wants it. langgraph sits installed but unused — whether it ever earns its place is an open question.
- **WP4 (still parked — entry criteria, not specs):** knowledge-query helper, staleness propagation to neighbors, type extensibility (`propose_type`). (Its semantic-search item shipped as WP11; its multi-user item was superseded by WP8.)
