# graph-context-mcp

An MCP server exposing a knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph is the source of truth; the LLM builds it and writes from it. The framing is selectable ([domain profiles](#domain-profiles-gc_profile)): a **story world** (characters, locations, events, rendered prose — the original and default surface) or a **work knowledge base** (people, teams, projects, meetings, decisions). See `docs/` (proposal) for the full design.

This repository contains the vertical slice (WP0), the **Anytype adapter (WP1)**, the **MCP tool layer (WP2)**, and the **story layer (WP3)**: an async `GraphRepository` port with two certified implementations (in-memory fake and `AnytypeGraphRepository`), a contract test suite that runs against both, a sync engine (hydrate + incremental resync with self-write suppression), `MockAnytype` (an in-process simulator of the documented local API), a running FastMCP stdio server exposing the eight v1 tools, body-backed node descriptions ([ADR 010](docs/adr/010-descriptions-in-the-body.md)), write-once-by-policy Prose bodies, and debounced `SessionContext` persistence behind a `SessionStore` port.

**Space-reflecting (v2, [ADR 006](docs/adr/006-space-reflecting-open-schema.md)):** the server reflects your *existing* Anytype space — native types (`character`, `event`, …) are nodes and every `objects`-format relation (yours or bootstrapped) is a labelled edge. There is no closed `gc_` vocabulary anymore; `gc_` keys survive only for infrastructure (Prose, SessionContext, and the stale-flag/story-time/fields scalars — summaries live in the built-in `description` property, [ADR 011](docs/adr/011-summary-in-builtin-description.md), and long-form descriptions in the body, [ADR 010](docs/adr/010-descriptions-in-the-body.md)). Architecture decisions live in [`docs/adr/`](docs/adr/).

**Live-server status:** the WP1 spike was run against a real local Anytype (API `2025-11-08`, 2026-06-21) and the assumption-driven corrections are applied in `infrastructure/anytype/` (resync via `POST /search`, endpoint-split page caps, timestamps-from-properties, `plural_name` on type creation). Node descriptions live in the object **body** ([ADR 010](docs/adr/010-descriptions-in-the-body.md)): created via `body`, updated via the `markdown` PATCH key (A7 — the S6 "write-once" finding was corrected 2026-07-02), fetched on demand and never hydrated. The mapping assumptions A1–A8 in `mapping.py` are mirrored by `mock_server.py`; a live-gated E2E suite (`ANYTYPE_E2E=1`) runs the same contracts against a real server.

Try it: `PYTHONPATH=src python scripts/demo_wp2_tools.py` — drives the full tool loop in-process (composite create → scene-assembly `explore` → `find_path` → stale-summary sweep → resync reporting → actionable errors) against the mock-backed repository.

```
pip install -e ".[dev]"
pytest          # mock-backed suite + live-gated E2E (ANYTYPE_E2E=1); live server not required
ruff check src tests
```

## Domain profiles (GC_PROFILE)

The schema is space-reflecting and domain-neutral (ADR 006); what a profile changes is **framing**: the tool docstrings the LLM reads (they are prompts) and a few native type-key → role mappings. Wire format never changes — storage keys (`gc_story_time`, `gc_prose`, …), tool names, and parameters are identical across profiles, so switching profiles never migrates data.

| | `fiction` (default) | `workspace` |
|---|---|---|
| Framing | characters, scenes, foreshadowing | people, projects, meetings, decisions |
| Worked examples | scene assembly, rendering prep | meeting/decision briefs, deep context |
| Extra Event-role types | — (`event` is already mapped) | `meeting`, `decision`, `milestone` (timeline over real time: epoch seconds or YYYYMMDD in `story_time`) |

Select with `GC_PROFILE=workspace` (unset = `fiction`; existing setups see zero change). Try the work-KB surface in-process:

```
PYTHONPATH=src python scripts/demo_workspace_profile.py
```

Profile docstrings are pinned by golden snapshot tests (`tests/interface/golden/`) — editing them is prompt engineering, and the golden diff is the review artifact (`GC_REGEN_GOLDENS=1 pytest tests/interface/test_profiles.py` to regenerate deliberately).

## Running the MCP server

The server speaks **stdio** (one process per client; no network port). Run it directly only for a quick local check:

```
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server   # dev: in-memory, nothing persists
```

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

Tools exposed: `context`, `create_node`, `update_node`, `get_node`, `explore`, `find_path`, `find_node`. (Prose/artifact capture is the orchestrator harness's job — WP7 auto-capture; there is no capture tool.) Every node parameter accepts a node **name** as well as an id (ambiguous names report their candidates); `find_node` covers browsing and disambiguation. Every response is prefixed with a `[project | focus | recent]` context header; validation errors echo the allowed values (they are written for an LLM to self-correct). Tool docstrings are prompts — see `interface/server.py`. **Cold start:** a fresh session has an empty focus stack, so traversal has nothing to default to; `context action="overview"` (alias `map`) returns a *derived* entry-point map — per-type counts plus the highest-degree "hub" nodes with their ids — to seed the first `explore`/`get_node`/`focus`. It is rebuilt from the graph each call (no maintained root node).

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

**The rule:** imports only point left-to-right along the arrows. Domain imports nothing but itself and `errors`. Application imports domain + ports. Infrastructure implements ports. Nothing imports infrastructure except the composition root (`interface/server.py`) and tests.

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
| `application/node_writer.py` | `create_node` / `update_node` use-case | Owns the summary-staleness rule; touches focus |
| `application/node_reader.py` | `get_node` use-case | Grouped edges + WP7 `include_provenance` excerpts |
| `application/explorer.py` | `explore` / `find_path` use-case | Resolves focus-stack defaults |
| `application/prose_recorder.py` | Capture service (orchestrator-called) | Write-once-by-policy body, explicit references |
| `application/session_persister.py` | Debounced session persistence | Flush every N / on shutdown; lenient `load_or_fresh` |
| `infrastructure/memory/` | `InMemoryGraphRepository` + `InMemorySessionStore` | Reference impls; certified by `tests/contract` |
| `infrastructure/anytype/client.py` | Async httpx client | Auth, version pin, pagination, bounded retry; `request_count` for budget asserts |
| `infrastructure/anytype/mapping.py` | The quirk quarantine | All representation assumptions (A1–A8) live here |
| `infrastructure/anytype/registry.py` | `SpaceRegistry`: the space's live types/relations | Resolves requested types & relation labels to existing keys; unknown labels surface for approval |
| `infrastructure/anytype/schema_bootstrap.py` | Idempotent **infra-only** bootstrap | gc_ infra types (Prose, SessionContext), scalar gc_ properties, starter `gc_edge_*` relations — story entities use the space's native types |
| `infrastructure/anytype/sync.py` | Hydrate / resync engine | Lenient reads, strict writes; search-based modified-since |
| `infrastructure/anytype/repository.py` | `AnytypeGraphRepository` | Persist-first write-through; composite rollback; self-write suppression |
| `infrastructure/anytype/session_repository.py` | `AnytypeSessionStore` | Snapshot JSON in a `SessionContext` meta-node's property |
| `infrastructure/anytype/mock_server.py` | `MockAnytype` | Spike-pinned behavior simulator (search caps, body-editing quirks, timestamps) |
| `interface/presenters.py` | Context header + detail levels + node/path views | Response-budget shaping lives at the edge, not in tested logic |
| `interface/tools.py` | The eight tools (SDK-free) | `guarded` wrapper: header + actionable errors + per-call logging |
| `interface/server.py` | Composition root | Only module importing infrastructure + the MCP SDK; lifespan wiring |

## Conventions to carry forward

1. **Business rules live in exactly one place.** Edge legality → `GraphIndex.add_edge`. Creation invariants → `schema.validate_new_node`. Staleness → `NodeWriter`. If you find yourself re-checking a rule in a second layer, the rule is in the wrong place.
2. **Domain stays pure.** No I/O, no `httpx`, no MCP types, no clocks. This is why `tests/unit` runs in milliseconds and can test traversal semantics exhaustively.
3. **Services take dependencies via constructor**, typed against ports. New tool ≈ new service module with the same shape as `Explorer`.
4. **Parameter objects mirror the tool surface.** `ExploreQuery` is the `explore` tool's schema in domain form; keep them in lockstep.
5. **Errors derive from `GraphContextError`** and carry actionable messages — the consumer of every error string is an LLM deciding what to do next.
6. **Fakes are contracts.** A behavior added to the Anytype adapter must land in `InMemoryGraphRepository` too, with a test. If the fake can't express it, the port is wrong — fix the port.
7. **Test names state behavior** (`test_failed_link_rolls_back_the_created_node`), grouped in classes per scenario; fixtures build worlds through public APIs, never by poking internals.

## Status & what's next

WP0–WP3 are complete and green against the mock (and the live-gated E2E suite): the Anytype adapter, the eight-tool MCP server, write-once Prose bodies, and debounced `SessionContext` persistence. The WP1 live-server spike has been run and its corrections applied (see "Live-server status" above and `docs/WORK_PACKAGES.md`), and the space-reflecting pivot ([ADR 006](docs/adr/006-space-reflecting-open-schema.md)) landed after WP3 — see the status addendum in `docs/WORK_PACKAGES.md`. Definition of Done holds: `pytest`, `ruff`, and `mypy --strict` are all clean; CI runs them plus `lint-imports` on every push.

WP4 (parked — entry criteria, not specs; see `docs/WORK_PACKAGES.md`): knowledge-query helper, staleness propagation to neighbors, type extensibility (`propose_type`), multi-user `SessionContext`, semantic search over summaries.
