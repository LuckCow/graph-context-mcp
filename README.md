# graph-context-mcp

An MCP server exposing a knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph is the source of truth; the LLM builds it and writes from it. The framing is selectable ([domain profiles](#domain-profiles-gc_profile)): a **story world** (characters, locations, events, rendered prose — the default), a **work knowledge base** (people, teams, projects, meetings, decisions), or a **personal assistant** (tasks, procedures, notes).

The stack, storage core up: an async `GraphRepository` port with two certified implementations (in-memory fake and `AnytypeGraphRepository`, with hydrate/resync and self-write suppression), a FastMCP stdio server exposing eight tools, and an **orchestrator harness** above it — a real Claude driver on your subscription, configurable activity modes ([ADR 015](docs/adr/015-configurable-activity-modes.md)), automatic per-turn provenance, semantic search with graph-aware ranking ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)/[016](docs/adr/016-graph-aware-ranking.md)), and chat transports for Discord and Anytype's own in-space chat ([ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)).

**Space-reflecting ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)):** the server reflects your *existing* Anytype space — native types (`character`, `event`, …) are nodes and every `objects`-format relation is a labelled edge. There is no closed `gc_` vocabulary; `gc_` keys survive only for infrastructure (Prose, SessionContext, and a few scalars — summaries live in the built-in `description` property, [ADR 011](docs/adr/011-summary-in-builtin-description.md), long-form descriptions in the body, [ADR 010](docs/adr/010-descriptions-in-the-body.md)).

Docs: [`docs/adr/`](docs/adr/) (decisions), [`docs/WORK_PACKAGES.md`](docs/WORK_PACKAGES.md) (roadmap + status), [`docs/TESTING.md`](docs/TESTING.md) (suites, live E2E, golden snapshots, demo scripts).

```bash
pip install -e ".[dev]"    # Python >= 3.11
pytest                     # mock-backed suite; no live server needed
```

Try it without Anytype: `PYTHONPATH=src python scripts/demo_wp2_tools.py` drives the full tool loop in-process against the mock-backed repository.

## Domain profiles (GC_PROFILE)

The schema is space-reflecting and domain-neutral; a profile changes **framing and behavior configuration**: the tool docstrings the LLM reads (they are prompts), type-key → role mappings, the orchestrator's **activity modes** (each a goal prompt + tool binding + capture policy), and the **timeline source**. Tool names and parameters are identical across profiles.

| | `fiction` (default) | `workspace` | `assistant` |
|---|---|---|---|
| Framing | characters, scenes, foreshadowing | people, projects, meetings, decisions | tasks, procedures, notes |
| Activity modes | `world_modeling`, `authoring` (captures prose) | `world_modeling`, `authoring` (captures drafts) | `organizing`, `record_procedure` (captures `procedure` nodes), `meeting_notes` (captures `note` nodes) |
| Timeline | `gc_story_time` (a story number) | `gc_story_time` (epoch/YYYYMMDD) | `event_date` (real ISO dates; `as_of="2026-07-04"`) |

Select with `GC_PROFILE=<name>` (unset = `fiction`). Deployments can add or override activity modes via a `GC_MODES_FILE` TOML file — *Record Procedure is a configuration entry, not a fork*.

### Editing modes inside Anytype

Modes are also configurable **in the space itself**: every object of the bootstrap-minted **Activity Mode** type defines one mode (the first run seeds an *Example Mode* template whose body walks through the fields):

- The object **name** becomes the `/mode` name ("Faithful Scribe" → `/mode faithful_scribe`); naming it after a built-in overrides that mode.
- The **page body** is the goal prompt the model follows.
- Tick **`gc_mode_mutating`** to allow graph edits; fill **`gc_capture_type`** (plus optional `gc_capture_references`, `gc_capture_min_chars`) to auto-capture substantial replies; **archive** the object to disable the mode.
- Edits apply when `/mode` is next used in chat, no restart. Precedence: profile defaults < `GC_MODES_FILE` < in-space.

## Running the MCP server

The server speaks **stdio** (one process per client; no network port). Run it directly only for a quick local check:

```
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server   # dev: in-memory, nothing persists
```

Tools exposed: `context`, `create_node`, `update_node`, `get_node`, `explore`, `find_path`, `find_node`, `query`. Every node parameter accepts a node **name** as well as an id (ambiguous names report their candidates); validation errors echo the allowed values — they are written for an LLM to self-correct. Tool docstrings are prompts (`interface/server.py`). **Cold start:** `context action="overview"` returns a derived entry-point map (per-type counts + highest-degree hubs) to seed the first `explore`/`get_node`/`focus`.

## Running the orchestrator (CLI / Discord / Anytype chat)

The orchestrator is the agentic harness over the same tool surface: a driver decides, activity modes bind tools, provenance records each mutating turn. Every transport shares one runtime assembly (`orchestrator/bootstrap.py`) and differs only in its message loop.

```
python -m graph_context.orchestrator.serve                                  # everything: Anytype bot + Discord (if configured) + turn-log viewer
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.orchestrator.cli   # keyboard loop; dev backend
python -m graph_context.orchestrator.discord_bot                            # Discord bot standalone
python -m graph_context.orchestrator.anytype_chat_bot                       # Anytype in-space chat bot standalone
```

`serve` is the consolidated entry point: one process running the Anytype chat bot (always), the Discord bot (only when the token file has content **and** at least one channel is bound — an empty secret file or a zero-table channels file is the "Discord off" switch), and the turn-log viewer in a daemon thread. One transport's crash takes the whole process down loudly; restarts belong to the supervisor.

`GC_DRIVER=claude` (default) talks to the model on your Claude subscription (`GC_DRIVER_MODEL` / `GC_DRIVER_EFFORT` tune it); `GC_DRIVER=manual` is the keyboard stand-in (`/tool <name> {json}`). The **mode** is per-chat (`/mode <name>` switches it). Provenance is on by default (`GC_PROVENANCE=0` disables; `GC_STORE_LLM_INPUT=0` withholds prompt text from intent nodes).

### Anytype chat (the all-in path)

The bot chats *inside* your Anytype spaces — the same store that holds the graph — replying in plain text with every referenced object attached as a clickable card ([ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)). Served spaces are declared in `spaces.toml` (`GC_SPACES_FILE`), keyed by space id, with optional `profile` / `project` / `modes_file` / `chat_id` / `exclude_chats`; every chat in a bound space is its own session/thread ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)), with live discovery of new chats. The chat cursor persists (`GC_CHAT_CURSOR`), so messages sent while the bot was down are answered on the next startup. The bot runs on its own headless node (the `anytype` compose sidecar) and posts as `graph-context-bot`. Never bind one space in both `spaces.toml` and `channels.toml`. Setup: see [Graduating to the live Anytype backend](#graduating-to-the-live-anytype-backend).

### Discord

The bot reads its token from `DISCORD_BOT_TOKEN_FILE` and serves **only** the channels you configure (no channel config = serve nowhere, loudly). Two configuration shapes ([ADR 017](docs/adr/017-channel-bound-spaces.md)) — setting both fails at startup:

- **`GC_DISCORD_CHANNELS`** (legacy allowlist): every listed channel shares the one env-configured runtime (`ANYTYPE_SPACE_ID`, `GC_PROFILE`).
- **`GC_CHANNELS_FILE`** (channel-bound spaces): a TOML file mapping each channel to its **own Anytype space** with optional per-channel `profile`, `project` label, and `modes_file`; each channel gets a fully independent runtime. One channel per space.

```toml
[channels.1523551542123298896]
space_id   = "bafyre..."           # required
profile    = "fiction"             # optional; defaults to GC_PROFILE
project    = "Ashfall"             # optional cosmetic label
modes_file = "ashfall-modes.toml"  # optional; overrides GC_MODES_FILE for this channel
```

It connects outbound via the Gateway websocket, so it runs inside the firewalled devcontainer; the **Message Content** privileged intent must be enabled in the Discord developer portal or every message arrives empty.

### Turn log

Every turn — user message, each model decision, every tool call with complete output, final replies — is written to a size-capped JSONL diary: `GC_TURN_LOG` sets the path (default `logs/turns.jsonl`; `0` disables), `GC_TURN_LOG_MAX_BYTES` the cap (default ~10 MB, oldest entries drop).

The viewer runs automatically inside `serve` (the devcontainer publishes it to the host at `http://127.0.0.1:8765/`), or standalone via `python -m graph_context.orchestrator.turn_log_server`: a dependency-free web UI grouping the diary into one collapsible card per user request, live-tailing new turns via SSE (filter by session/mode, search, errors-only). Point it elsewhere with `--log` / `--port` or `GC_LOG_VIEWER_HOST` / `GC_LOG_VIEWER_PORT`. No server needed either: open `src/graph_context/orchestrator/turn_log_viewer.html` directly in a browser and pick a `turns.jsonl` file.

## Connecting Claude Desktop (from the dev container)

Claude Desktop runs on your **host**; the server runs **inside the dev container**, so Claude Desktop starts it *inside the already-running container* over stdio with `docker exec -i`.

**1. Start the container** (it must be running before Claude Desktop launches the server):

```
docker compose -f .devcontainer/docker-compose.yml up -d --build
```

**2. Add the server to Claude Desktop's config** (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows: `%APPDATA%\Claude\claude_desktop_config.json`; a copy-paste entry lives at [`.devcontainer/claude_desktop_config.example.json`](.devcontainer/claude_desktop_config.example.json)):

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

**3. Restart Claude Desktop.** You should see the eight tools in the tools menu. This first smoke test uses `GC_BACKEND=memory` — no Anytype, nothing persists. `docker` must be on Claude Desktop's `PATH`.

### Graduating to the live Anytype backend

`GC_BACKEND=anytype` (the default) talks to the **headless Anytype sidecar** — the `anytype` compose service running a bot account. The container already wires everything (`ANYTYPE_API_BASE_URL=http://anytype:31012`, `ANYTYPE_API_KEY_FILE`, `ANYTYPE_SPACE_ID`), and `docker exec` inherits it all, so the live backend needs **no `-e` flags**: drop the `GC_BACKEND=memory` override from the JSON above and you're live. To point at a different space, add `-e ANYTYPE_SPACE_ID=…`. Your desktop app is the *human* surface: it shares spaces with the bot over Anytype's sync network and never talks to this stack directly.

#### One-time sidecar bootstrap (bot account)

The sidecar runs `anytype serve` with the API on port 31012 and the write rate limit disabled. Its identity/config (`~/.config/anytype`) and object store (`~/.anytype`) are named volumes, so they survive rebuilds; **both** mounts are required — with only one, a rebuild wipes the bot's keys. Setup, from the host:

```bash
# 1. Build + start the stack (the sidecar is part of it)
docker compose -f .devcontainer/docker-compose.yml up -d --build

# 2. Create the bot account (once) and an API key
docker exec -it graph-context-mcp-anytype anytype auth create graph-context-bot
docker exec -it graph-context-mcp-anytype anytype auth apikey create "graph-context"
```

Back up the **account key** printed by `auth create` to `.devcontainer/secrets/anytype_account_key` (it is the bot's identity), and paste the **API key** into `.devcontainer/secrets/anytype_api_key`. Then run `docker compose … up -d` once more so `dev` remounts the key (secret mounts go stale when the file's inode changes).

#### Sharing spaces with the bot

For every space the bot should serve: create an invite link in the desktop app, then

```bash
docker exec -it graph-context-mcp-anytype anytype space join "<invite-link>"
docker exec -it graph-context-mcp-anytype anytype space list   # wait until synced
```

approve the join request in the desktop app and grant **Editor**. Space ids are identical for every member, so copy them straight into `spaces.toml` (chat transport) or `channels.toml` (Discord) — never both for one space. Sanity check from the dev container:

```bash
curl -s http://anytype:31012/v1/spaces -H "Anytype-Version: 2025-11-08" \
  -H "Authorization: Bearer $(cat /run/secrets/anytype_api_key)"
```

## Semantic search (GC_EMBEDDER)

"Find the node I'm describing" — as a **derived projection**, never a new source of truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)). `GC_EMBEDDER` selects the embedder: `off` (default), `hash` (deterministic, used by the test suite), or `local` (sentence-transformers over the image-baked `BAAI/bge-small-en-v1.5`). Embeddings live in a disposable SQLite cache (`GC_SEMANTIC_CACHE`) keyed by `(node_id, content_hash, model)`.

When enabled, `find_node` gains a third tier (exact → substring → semantic, hits labelled so the LLM knows it holds a fuzzy match) and `NodeNotFound` errors append "closest by meaning" candidates. Retrieval runs through the **Ranker** ([ADR 016](docs/adr/016-graph-aware-ranking.md)): semantic recall seeds, graph expansion recruits, spreading activation scores, and every hit carries a nameable evidence line. **Non-feature by decision:** semantic never silently resolves a mutation target — exact resolves, semantic *suggests*.

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

**The rule:** imports only point left-to-right along the arrows. Nothing imports infrastructure except the composition roots — `interface/server.py` and the orchestrator's (`cli.py`, `bootstrap.py`, `discord_bot.py`, `anytype_chat_bot.py`, and `serve.py`), all delegating to the shared service builder `composition.py` — and tests. The orchestrator is a **second interface adapter** ([ADR 007](docs/adr/007-orchestrator-second-interface-adapter.md)): it reuses `interface/tools.py` but never the MCP module, and agent/transport frameworks (claude-agent-sdk, discord.py) never leak outside it. All of this is machine-enforced: import-linter contracts in `pyproject.toml` fail CI on violation.

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
| `ports/session_store.py` | Keyed session-snapshot contract | Plain-dict snapshots per required key ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)); lenient load (corrupt → `None`) |
| `ports/mode_store.py` | Activity-Mode config contract | Plain payload dicts; validation lives in the loader, not the store |
| `ports/semantic.py` | `Embedder` + `SemanticIndex` contracts | Embeddings are a cache keyed by content hash + model, never truth ([ADR 014](docs/adr/014-semantic-search-as-derived-projection.md)) |
| `application/node_writer.py` | `create_node` / `update_node` use-case | Owns the summary-staleness rule; touches focus |
| `application/node_reader.py` | `get_node` use-case | Grouped edges + `include_provenance` excerpts |
| `application/explorer.py` | `explore` / `find_path` use-case | Resolves focus-stack defaults |
| `application/capture_recorder.py` | Capture service (orchestrator-called) | Policy-typed artifacts ([ADR 015](docs/adr/015-configurable-activity-modes.md)); native types are first-class, only `gc_prose` keeps infra hiding |
| `application/mutation_journal.py` | Writers report created/modified ids at the source | `NullJournal` in the MCP server; drained per turn in the orchestrator |
| `application/intent_recorder.py` | One `gc_intent` node per mutating turn | Provenance is a harness responsibility ([ADR 008](docs/adr/008-provenance-as-harness-responsibility.md)) |
| `application/semantic_projector.py` | The embedding cache tracks the graph | Full pass + prune after hydrate; incremental from resync; store touches never re-embed |
| `application/ranker.py` | Graph-aware retrieval ([ADR 016](docs/adr/016-graph-aware-ranking.md)) | Recall seeds → graph recruits → activation scores; every hit carries evidence |
| `application/session_persister.py` | Debounced session persistence | Flush every N / on shutdown; lenient `load_or_fresh`; keyed |
| `application/session_registry.py` | The one source of live sessions ([ADR 021](docs/adr/021-per-chat-keyed-sessions.md)) | Lazy keyed `(SessionState, persister)` cache; `flush_all` at teardown |
| `composition.py` | Shared service builder | One wiring; all composition roots delegate to it |
| `infrastructure/memory/` | In-memory repository, session store, mode store | Reference impls; certified by `tests/contract` |
| `infrastructure/semantic/` | Hash + sentence-transformers embedders; memory + SQLite index | `GC_EMBEDDER` selects; the SQLite cache file is disposable |
| `infrastructure/anytype/client.py` | Async httpx client | Auth, version pin, pagination, bounded retry; `request_count` for budget asserts |
| `infrastructure/anytype/mapping.py` | The quirk quarantine | All representation assumptions (A1–A8) live here |
| `infrastructure/anytype/registry.py` | `SpaceRegistry`: the space's live types/relations | Resolves requested types & relation labels to existing keys; unknown labels surface for approval |
| `infrastructure/anytype/schema_bootstrap.py` | Idempotent **infra-only** bootstrap | gc_ infra types (Prose, SessionContext, Activity Mode), scalar gc_ properties, starter `gc_edge_*` relations — story entities use the space's native types |
| `infrastructure/anytype/sync.py` | Hydrate / resync engine | Lenient reads, strict writes; search-based modified-since |
| `infrastructure/anytype/repository.py` | `AnytypeGraphRepository` | Persist-first write-through; composite rollback; self-write suppression |
| `infrastructure/anytype/session_repository.py` | `AnytypeSessionStore` | Snapshot JSON in a per-key `SessionContext` node (discriminated by `gc_session_key`) |
| `infrastructure/anytype/mode_store.py` | `AnytypeModeStore` | One mode per `gc_activity_mode` object: name → `/mode` slug, page body → goal, archive = disable |
| `infrastructure/anytype/chat.py` | Chat quirk quarantine + `AnytypeChatClient` | Chat payload/SSE assumptions (C1–C6); the chat analogue of `mapping.py` |
| `infrastructure/anytype/mock_server.py` | `MockAnytype` | Spike-pinned behavior simulator (search caps, body-editing quirks, timestamps, chat routes + live SSE) |
| `interface/presenters.py` | Detail levels + node/path views | Response-budget shaping lives at the edge, not in tested logic |
| `interface/tools.py` | The eight tools (SDK-free) | `guarded` wrapper: actionable errors + per-call logging |
| `interface/context_block.py` | Turn-start context block ([ADR 020](docs/adr/020-curated-cross-turn-context.md)) | Scratchpad + working-set buckets + recent trail, once per turn, budget-degraded |
| `interface/profiles.py` | Domain profiles + `ModeSpec` defaults | Docstrings are prompts; golden-pinned per profile |
| `interface/server.py` | MCP composition root | Only module importing the MCP SDK; lifespan wiring |
| `orchestrator/pipeline.py` | `handle_message` turn loop | Per-turn tool budget; opens with the context block + conversation memory (`/clear` resets it); drains the journal into an intent node at turn end |
| `orchestrator/modes.py` | `ModeSpec` loader (profile < `GC_MODES_FILE` < in-space) | Unbound tools don't exist in the session — unavailable, not refused; `/mode` re-reads all sources |
| `orchestrator/drivers.py` | `LLMDriver` seam + scripted/manual drivers | Transcript + tool docs + mode goal in; tool calls or a reply out |
| `orchestrator/claude_driver.py` | The real model behind the seam | claude-agent-sdk on your Claude subscription; the SDK never executes tools — calls are harvested and returned as the decision |
| `orchestrator/capture.py` | Authoring auto-capture | Exact-name entity linking; the harness records what tools used to ask for |
| `orchestrator/turn_log.py` | Full-fidelity turn diary (JSONL) | Input, every driver decision, every tool call + complete output, final replies; byte-capped — oldest entries drop |
| `orchestrator/turn_log_server.py` | Turn-log viewer HTTP server | Stdlib-only; SSE live tail with shrink→reset; hosts the packaged `turn_log_viewer.html` |
| `orchestrator/serve.py` | Consolidated composition root | One process: Anytype bot + Discord bot (if configured) + viewer thread; fail-together |
| `orchestrator/bootstrap.py` | Orchestrator runtime wiring | Shared by every transport; `GC_DRIVER` / `GC_PROVENANCE` / `GC_TURN_LOG` resolution; one runtime per channel or space binding |
| `orchestrator/channels.py` | Channel→space bindings (`GC_CHANNELS_FILE`, [ADR 017](docs/adr/017-channel-bound-spaces.md)) | Plain parsing/validation; one channel per space, enforced at startup |
| `orchestrator/discord_transport.py` + `discord_bot.py` | Discord adapter | Per-message policy is plain logic; only the composition-root shim imports discord.py |
| `orchestrator/spaces.py` | Space→chat bindings (`GC_SPACES_FILE`, [ADR 019](docs/adr/019-anytype-chat-transport-and-headless-sidecar.md)/[021](docs/adr/021-per-chat-keyed-sessions.md)) | Table key IS the space id; serve-all-chats minus `exclude_chats`, or a `chat_id` pin |
| `orchestrator/rendering.py` | Shared reply rendering | `render` prefixes + `chunk`ing, shared by the chat transports |
| `orchestrator/anytype_chat_transport.py` + `anytype_chat_bot.py` | Anytype in-space chat adapter | Echo suppression, persisted chat cursor (offline catch-up), `anytype://` deep links; only the composition root touches infrastructure |

## Conventions

The working conventions live in [CLAUDE.md](CLAUDE.md) (CLEAN principles and their repo-specific applications). The load-bearing ones: business rules live in exactly one place; the domain stays pure (no I/O, no clocks — `tests/unit` runs in milliseconds); fakes are contracts (adapter behavior lands in the fake too, or the port is wrong); every tool response and error string is written for an LLM to act on.

## Status & what's next

Full history and specs live in [`docs/WORK_PACKAGES.md`](docs/WORK_PACKAGES.md). Shipped: the storage core and eight-tool MCP server, the space-reflecting pivot, domain profiles, the orchestrator harness with the real Claude driver, automatic provenance + auto-capture, body descriptions and attribute reflection, semantic search + graph-aware ranking, configurable activity modes, the Discord and Anytype chat transports, and per-chat keyed sessions. Definition of Done holds: `pytest`, `ruff`, `mypy --strict`, and `lint-imports` are all clean; CI runs exactly these on every push.

Open work, in rough order of proximity: the WP8 multi-user remainder (per-user sessions *within* one space, per-user mode authorization/consent, Telegram/Slack transports, queue fairness); WP11 stage 2 (passage-level search, reranker adapters, the Voyage embedder, the `off`→`local` embedder default flip, RAG prefetch); cross-turn driver memory (each `decide()` is deliberately a fresh stateless session for now); and the parked WP4 items (knowledge-query helper, staleness propagation, `propose_type`).
