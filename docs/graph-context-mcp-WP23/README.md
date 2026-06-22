# graph-context-mcp

An MCP server exposing a story-world knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph is the source of truth for characters, locations, events, and rendered prose; the LLM builds the world and renders scenes from it. See `docs/` (proposal) for the full design.

This repository contains the vertical slice (WP0) **plus the Anytype adapter (WP1)**: an async `GraphRepository` port with two certified implementations (in-memory fake and `AnytypeGraphRepository`), a contract test suite that runs against both, a sync engine (hydrate + incremental resync with self-write suppression), and `MockAnytype` — an in-process simulator of the documented local API used in place of a live server. The MCP tool layer (WP2) is next; see `docs/WORK_PACKAGES.md`.

**WP2/WP3 status:** scaffolded — all seven MCP tools, the FastMCP composition root (`python -m graph_context.interface.server`, `GC_BACKEND=memory` for no-Anytype dev), Prose recording with write-once bodies, and session persistence plumbing are in place and runnable (`PYTHONPATH=src python scripts/demo_wp2_tools.py`). The finishing worklist for junior devs is `docs/HANDOFF_WP2_WP3.md`.

**Important:** no live Anytype server was available during WP1. Our representation assumptions are explicitly registered as A1–A4 in `infrastructure/anytype/mapping.py` and mirrored by `mock_server.py`; validating them against a live server (the spike in `docs/WORK_PACKAGES.md`) is the first task for whoever has one, and corrections must land in both files in the same PR.

Try it: `PYTHONPATH=src python scripts/demo_wp1.py` — bootstrap, composite writes, restart-and-hydrate equality, out-of-band human edits picked up by resync, all against the mock.

```
pip install -e ".[dev]"
pytest          # 79 tests; live server not required (MockAnytype)
ruff check src tests
```

## Architecture in one paragraph

Anytype is durable storage and the *human editing surface* — but its API only text-searches names/snippets, so all graph traversal happens in an in-memory `GraphIndex`: a **derived, rebuildable projection** that repository adapters keep coherent (write-through on our writes, hydrate/resync for edits humans make directly in the Anytype UI). Everything above storage follows a strict dependency rule:

```
interface  ──▶  application  ──▶  domain
   (MCP tools,      (use-cases,       (pure logic:
    presenters)      one per tool)     graph, traversal,
                          │            schema, session)
                          ▼
                       ports  ◀──implemented by──  infrastructure
                  (GraphRepository)                 (memory fake today,
                                                     Anytype adapter next)
```

**The rule:** imports only point left-to-right along the arrows. Domain imports nothing but itself and `errors`. Application imports domain + ports. Infrastructure implements ports. Nothing imports infrastructure except the composition root (the future `interface/server.py`) and tests.

## Layout

| Path | Role | Key idea |
|---|---|---|
| `domain/schema.py` | Fixed v1 vocabulary + structural rules | Closed set; edge endpoint rules enforced once, at `GraphIndex.add_edge` |
| `domain/models.py` | `Node`, `Edge`, `NodeDraft`, `LinkSpec` | Immutable; ids minted by storage, hence draft vs node |
| `domain/graph.py` | `GraphIndex` adjacency projection | The traversal engine's substrate; rebuildable, never authoritative |
| `domain/traversal.py` | Bounded BFS (`explore`) | Pure function; filters prune subtrees; `as_of` hides future events |
| `domain/pathfinding.py` | Bounded shortest path (`find_path`) | Undirected walk, direction-preserving result |
| `domain/session.py` | `FocusStack`, `RecentHistory`, `SessionState` | Working *set* not a pointer; pinning; top never evicted |
| `ports/graph_repository.py` | Persistence contract | Composite-create **rollback contract** documented here |
| `application/node_writer.py` | `create_node` / `update_node` use-case | Owns the summary-staleness rule; touches focus |
| `application/explorer.py` | `explore` / `find_path` use-case | Resolves focus-stack defaults |
| `infrastructure/memory/` | `InMemoryGraphRepository` | Reference implementation; certified by `tests/contract` |
| `infrastructure/anytype/client.py` | Async httpx client | Auth, version pin, pagination, bounded retry; `request_count` for budget asserts |
| `infrastructure/anytype/mapping.py` | The quirk quarantine | All representation assumptions (A1–A4) live here |
| `infrastructure/anytype/schema_bootstrap.py` | Idempotent space setup | gc_ types + edge relation properties |
| `infrastructure/anytype/sync.py` | Hydrate / resync engine | Lenient reads, strict writes; last-modified watermark |
| `infrastructure/anytype/repository.py` | `AnytypeGraphRepository` | Persist-first write-through; composite rollback; self-write suppression |
| `infrastructure/anytype/mock_server.py` | `MockAnytype` | Documented-behavior simulator; spike questions as knobs |
| `interface/presenters.py` | Context header + detail levels | Response-budget shaping lives at the edge, not in tested logic |

## Conventions to carry forward

1. **Business rules live in exactly one place.** Edge legality → `GraphIndex.add_edge`. Creation invariants → `schema.validate_new_node`. Staleness → `NodeWriter`. If you find yourself re-checking a rule in a second layer, the rule is in the wrong place.
2. **Domain stays pure.** No I/O, no `httpx`, no MCP types, no clocks. This is why `tests/unit` runs in milliseconds and can test traversal semantics exhaustively.
3. **Services take dependencies via constructor**, typed against ports. New tool ≈ new service module with the same shape as `Explorer`.
4. **Parameter objects mirror the tool surface.** `ExploreQuery` is the `explore` tool's schema in domain form; keep them in lockstep.
5. **Errors derive from `GraphContextError`** and carry actionable messages — the consumer of every error string is an LLM deciding what to do next.
6. **Fakes are contracts.** A behavior added to the Anytype adapter must land in `InMemoryGraphRepository` too, with a test. If the fake can't express it, the port is wrong — fix the port.
7. **Test names state behavior** (`test_failed_link_rolls_back_the_created_node`), grouped in classes per scenario; fixtures build worlds through public APIs, never by poking internals.

## Next work packages

**WP1 — Anytype adapter** (`infrastructure/anytype/`): `client.py` (httpx against `http://localhost:31009/v1`, bearer key, `Anytype-Version: 2025-11-08` header, pagination, retry), `schema_bootstrap.py` (idempotently ensure Types + one relation Property per `EdgeType`), `mapping.py` (Node ⇄ Anytype object/properties), `repository.py` (implements the port; write-through to `GraphIndex`; full hydrate + incremental resync via `last_modified_date`). **Spike first:** verify relation ("object") properties round-trip through create/PATCH and appear in list/search responses — that single fact decides whether hydration is one pass or N+1.

**WP2 — MCP tool layer** (`interface/`): FastMCP server, the 7 tool definitions as thin wrappers (validate params → call service → presenter + context header), composition root wiring repository/session/services.

**WP3 — Story layer**: `Prose` recording (`record_prose` service; text in the Anytype body, write-once), stale-summary listing/refresh, `SessionContext` persistence behind a `SessionStore` port.

Parked decisions (revisit, don't relitigate): staleness propagation to neighbors; `refresh_summary` as tool vs. usage pattern; semantic search over summaries; type extensibility.
