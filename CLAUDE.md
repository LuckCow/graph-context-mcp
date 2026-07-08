# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server exposing a story-world knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph (characters, locations, events, prose) is the source of truth; an LLM builds the world and renders scenes from it via eight stdio MCP tools. Design docs: `docs/adr/` (decisions), `docs/WORK_PACKAGES.md` (roadmap/status), README (setup + layout table).

## Environment constraints

The devcontainer applies an egress firewall (`.devcontainer/init-firewall.sh`): **most web connections are blocked**. Don't assume `curl`/`pip install <new-package>` will reach the internet. If a new package or dependency is needed, **ask the user first** — they will add it to the Docker container setup.

## Commands

```bash
pip install -e ".[dev]"        # install (Python >=3.11)

pytest                          # full mock-backed suite; live E2E self-skips
pytest tests/unit/test_traversal.py -k explore   # single file / test (pythonpath is configured in pyproject)
ruff check src tests            # lint
mypy src                        # strict typing
lint-imports                    # layer/dependency rule (import-linter contracts in pyproject.toml)
```

Definition of Done = all four green; CI (`.github/workflows/ci.yml`) runs exactly these on every push.

```bash
PYTHONPATH=src python scripts/demo_wp2_tools.py                              # drive the full tool loop in-process (mock-backed)
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server    # run the stdio server, nothing persists
python -m graph_context.orchestrator.anytype_chat_bot                        # chat inside Anytype spaces (spaces.toml, ADR 019)
```

### Live E2E (real Anytype server)

```bash
ANYTYPE_E2E=1 python -m pytest tests/e2e -q
```

Runs against the **headless sidecar** (`anytype` compose service, part of the default stack since the WP14 cutover, ADR 019): compose sets `ANYTYPE_API_BASE_URL=http://anytype:31012` and `ANYTYPE_API_KEY_FILE=.devcontainer/secrets/anytype_api_key` (the BOT account's key). The desktop app is not needed — the suite find-or-creates a space named exactly `GC-E2E` on the bot account and **resets it before and after each run** (the local API cannot delete spaces); spike artifacts do not survive a run, the spike scripts reseed themselves. With the sidecar's rate limit disabled the whole suite runs in ~10s (the old desktop endpoint throttled writes to ~1 req/s). The desktop app on the host remains the human surface; to point tooling at it temporarily, override `ANYTYPE_BASE_URL=http://host.docker.internal:31009` with a desktop-issued key.

## Architecture

Ports-and-adapters with a strict, **machine-enforced** dependency rule (import-linter contracts in `pyproject.toml` — violations fail CI):

```
interface ──▶ application ──▶ domain
(MCP tools,    (use-cases,     (pure logic: graph, traversal,
 presenters)    one per tool)    schema, session — no I/O, no clocks)
                    │
                    ▼
                  ports ◀── implemented by ── infrastructure
             (GraphRepository,               (in-memory fake +
              SessionStore)                   Anytype adapter)
```

Only the composition root (`interface/server.py`) and tests may import `infrastructure`; it is also the only module importing the MCP SDK.

Key ideas that span multiple files:

- **Anytype is storage + the human editing surface, never the traversal engine.** Its API only text-searches names/snippets, so all traversal runs on `domain/graph.py`'s `GraphIndex` — a derived, rebuildable projection. Repository adapters keep it coherent: write-through on our writes, hydrate/resync for edits humans make in the Anytype UI (with self-write suppression in `infrastructure/anytype/repository.py`).
- **Space-reflecting open schema (ADR 006).** The server reflects the user's *existing* space: native types are node types, every `objects`-format relation is a labelled edge. There is no closed `gc_` vocabulary; `gc_` keys survive only for infrastructure (Prose, SessionContext, and the stale-flag/story-time/fields scalars — summaries live in the built-in `description` property per ADR 011, long-form descriptions in the body per ADR 010). `infrastructure/anytype/registry.py` resolves requested types/relation labels against what the space actually has.
- **Fakes are contracts.** `tests/contract/` runs the same suite against `InMemoryGraphRepository` and the (mock-backed) `AnytypeGraphRepository`. Any behavior added to the Anytype adapter must land in the fake too, with a test; if the fake can't express it, fix the port. The live-gated `tests/e2e/` suite runs the same contracts against a real server.
- **Quirk quarantine.** All Anytype representation assumptions (A1–A8) live in `infrastructure/anytype/mapping.py`, mirrored by `mock_server.py` (`MockAnytype`), which pins live-server behavior confirmed by spikes (search caps, the body-editing field-name mismatch — create `body`, update `markdown`, never in list/search (ADR 010) — timestamps-from-properties, fresh-relation settle window).
- **The consumer of every tool response and error string is an LLM.** Tool docstrings are prompts (`interface/server.py`); errors derive from `GraphContextError` and echo allowed values so the model can self-correct. Response-budget shaping lives in `interface/presenters.py`, not in tested logic.
- **Cross-turn context is curated, echoed once per turn (ADR 020, WP15).** The model keeps a scratchpad (`context action="note"`, replaced wholesale) and a `WorkingSet` of explicitly held nodes in granularity buckets (≤2 `full` with body + one-hop edges, ≤6 `summaries`); `interface/context_block.py` renders them plus the automatic recent trail as the first transcript event of every orchestrator turn — once per turn, budget-degraded, never per-response (the old per-response header was removed 2026-07-06 as token waste). The pipeline's `ConversationMemory` replays prior user/assistant messages ahead of the block; `/clear` empties it via a persisted order-id watermark, never by deleting chat messages.

## Conventions

Follow **CLEAN** principles in all code: **C**ohesive (one reason to change per module), **L**oosely coupled (depend on ports/abstractions, not concretions), **E**ncapsulated (no reaching into internals — tests included), **A**ssertive (objects act on their own state rather than interrogating others'), **N**on-redundant (a rule or fact lives in exactly one place). The repo-specific rules below are applications of these:

1. **Business rules live in exactly one place** — edge legality in `GraphIndex.add_edge`, creation invariants in `schema.validate_new_node`, summary staleness in `NodeWriter`. Re-checking a rule in a second layer means it's in the wrong place.
2. **Domain stays pure**: no I/O, no `httpx`, no MCP types, no clocks — this keeps `tests/unit` in milliseconds.
3. Services take dependencies via constructor, typed against ports; a new tool ≈ a new application service shaped like `Explorer`.
4. Parameter objects mirror the tool surface (`ExploreQuery` ↔ the `explore` tool schema); keep them in lockstep.
5. Test names state behavior (`test_failed_link_rolls_back_the_created_node`), grouped in classes per scenario; fixtures build worlds through public APIs, never by poking internals.
