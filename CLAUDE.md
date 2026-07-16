# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server exposing a story-world knowledge graph backed by [Anytype](https://developers.anytype.io/). The graph (characters, locations, events, prose) is the source of truth; an LLM builds the world and renders scenes from it via nine stdio MCP tools. Design docs: `docs/adr/` (decisions), `docs/WORK_PACKAGES.md` (roadmap/status), `docs/TESTING.md` (suites, live E2E, goldens, behavioral evals, demo scripts), README (setup + layout table).

## Environment constraints

The devcontainer applies an egress firewall (`.devcontainer/init-firewall.sh`): **most web connections are blocked**. Don't assume `curl`/`pip install <new-package>` will reach the internet. If a new package or dependency is needed, **ask the user first** — they will add it to the Docker container setup.

## Commands

```bash
pip install -e ".[dev]"        # install (Python >=3.11)

pytest                          # full mock-backed suite; live E2E self-skips
pytest tests/unit/test_traversal.py -k explore   # single file / test (pythonpath is configured in pyproject)
ruff check src tests evals      # lint
mypy src                        # strict typing
lint-imports                    # layer/dependency rule (import-linter contracts in pyproject.toml)
```

Definition of Done = all four green; CI (`.github/workflows/ci.yml`) runs exactly these on every push.

```bash
PYTHONPATH=src python scripts/demo_wp2_tools.py                              # drive the full tool loop in-process (mock-backed)
python -m evals run                                           # behavioral evals (WP16, ADR 024): live-model runs, graded; --driver scripted replays without a model
GC_BACKEND=memory PYTHONPATH=src python -m graph_context.interface.server    # run the stdio server, nothing persists
python -m graph_context.orchestrator.serve                                   # everything: Anytype bot + Discord (if token non-empty AND channels bound) + turn-log viewer (published to the host at 127.0.0.1:8765)
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
- **Space-reflecting open schema (ADR 006).** The server reflects the user's *existing* space: native types are node types, every `objects`-format relation is a labelled edge. There is no closed `gc_` vocabulary; `gc_` keys survive only for infrastructure (Prose, SessionContext, and the stale-flag/story-time scalars — summaries live in the built-in `description` property per ADR 011, long-form descriptions in the body per ADR 010). Everything the server writes is a REAL Anytype property or the object body (ADR 028) — the old `gc_fields` JSON blob is retired: session snapshots live in `gc_chat_session`, recorder attribution in the minted `gc_generated_at`/`gc_user_id`/`gc_model`/`gc_mode`/`gc_origin` properties (`domain/attribution.py`). `infrastructure/anytype/registry.py` resolves requested types/relation labels against what the space actually has.
- **Fakes are contracts.** `tests/contract/` runs the same suite against `InMemoryGraphRepository` and the (mock-backed) `AnytypeGraphRepository`. Any behavior added to the Anytype adapter must land in the fake too, with a test; if the fake can't express it, fix the port. The live-gated `tests/e2e/` suite runs the same contracts against a real server.
- **Quirk quarantine.** All Anytype representation assumptions (A1–A8) live in `infrastructure/anytype/mapping.py`, mirrored by `mock_server.py` (`MockAnytype`), which pins live-server behavior confirmed by spikes (search caps, the body-editing field-name mismatch — create `body`, update `markdown`, never in list/search (ADR 010) — timestamps-from-properties, fresh-relation settle window).
- **The consumer of every tool response and error string is an LLM.** Tool docstrings are prompts (`interface/server.py`); errors derive from `GraphContextError` and echo allowed values so the model can self-correct. Response-budget shaping lives in `interface/presenters.py`, not in tested logic.
- **Cross-turn context is curated, echoed once per turn (ADR 020, WP15).** The model keeps a scratchpad (`context action="note"`, replaced wholesale) and a `WorkingSet` of explicitly held nodes in granularity buckets (≤2 `full` with body + one-hop edges, ≤6 `summaries`); `interface/context_block.py` renders them plus the automatic recent trail as the first transcript event of every orchestrator turn — once per turn, budget-degraded, never per-response (the old per-response header was removed 2026-07-06 as token waste). The pipeline's `ConversationMemory` replays prior user/assistant messages ahead of the block; `/clear` empties it via a persisted order-id watermark, never by deleting chat messages.
- **Sessions are keyed per chat (ADR 021, WP8).** Every session — each chat thread, Discord channel, the CLI, the MCP client — is addressed by an explicit key (`anytype:<chat_id>`, `discord:<channel_id>`, `mcp`, `cli`) and owns its own `SessionState` + one `gc_session_context` node discriminated by `gc_session_key`. There is no unkeyed/default session (`SessionStore.load/save` require the key). `application/session_registry.py` is the sole source of live sessions; `composition` exposes `services_for(key)`, and the pipeline hands each session id a `Services` view over the shared repository. The Anytype bot serves every chat in a bound space (minus `exclude_chats`) as its own thread, with live discovery of new chats; all chats of one space share one runtime (turns serialize per space). Mode is persisted per chat.
- **Scheduled Events are timed prompts stored as nodes (ADR 027, WP18).** A `gc_scheduled_event` node (infra role `ScheduledEvent`) carries `gc_schedule` (one-shot ISO local datetime or five-field cron), `gc_schedule_prompt`, a `gc_schedule_status` select (Pending fires; Completed/Cancelled inert; empty = Pending; cancel preserves the schedule — re-enable by flipping to Pending), `gc_last_fired`, and the target chat's `gc_session_key`. These are REAL properties with human display names (`GC_REFLECTED_FIELD_KEYS` overrides the registry's `gc_` reflection exclusion for this surface — the attribution stamps share it since ADR 028); humans author events directly in Anytype (an explainer object is seeded with the type), the LLM via the `schedule` tool (bound in every mode, like `context`). Timing rules are pure (`domain/scheduling.py`, dependency-free cron): recurring events fire only once ARMED via a first `gc_last_fired` stamp, downtime collapses to one late fire. Only the Anytype bot fires them: `_watch_schedule` marks the node fired (at-most-once, one-shots → Completed) then injects a turn through `handle_message` under the route lock (`run_scheduled` in the transport; no "Processing…" placeholder — nothing posts until the reply is ready).
- **Turn activity streams into the chat (ADR 029, WP19).** `Orchestrator.handle_message` takes an optional per-call `observer: TurnObserver | None` (async `turn_started`/`decision`/`tool_result`, fired beside the `turn_log` taps; observers must not raise; `/mode`//`/clear` turns never stream). The Anytype bot's sink (`orchestrator/turn_activity.py`) claims the "Processing…" placeholder as a live activity message edited in place (edits reach clients instantly over their SSE subscription; coalesced ≥2s apart for the ~1 req/s API budget), the reply posts fresh, and a last edit collapses the trace to `✓ n tool calls · m decisions`. Detail is a MODE property — `ModeSpec.activity_detail` (`off | minimal | tools | full`, default `minimal`), settable in profile specs, `GC_MODES_FILE`, or the Activity Mode object's `gc_mode_activity_detail` field — so switching modes switches verbosity; `off` = the pre-WP19 placeholder-becomes-reply lifecycle. The renderer (`ActivityLog`) alone interprets levels; scheduled turns and Discord don't stream.
- **Web search is a mode property, server-side only (ADR 030, WP20).** `ModeSpec.web_search` (default off; profile specs, `GC_MODES_FILE`, or the Activity Mode object's `gc_mode_web_search` checkbox) admits Anthropic's server-side search tool for that mode's decisions — the pipeline forwards the flag on every `decide()`, the subscription driver re-admits exactly the `WebSearch` built-in (ADR 007's `tools=[]` boundary otherwise intact), the API driver appends the `web_search_*` tool block and resumes `pause_turn` in-decide. Searches never execute on the harness (the egress firewall never sees them); they surface as `LLMTurn.server_tool_calls` in the turn log and activity stream — never as pipeline work. A searching decision's raw result payloads ride the decision event (WP22: `server_tool_results`, opaque JSON, turn-local like thinking), so the next decide replays what the search returned — verbatim blocks with `encrypted_content` intact on the API driver, a `search_digest` call/result pair in the text transcript on the subscription driver; the diary logs digests, never raw payloads.
- **Chats title themselves; spaces pick the starting mode (ADR 031, WP21).** After an untitled chat's first real exchange the bot makes one driver side-call and renames the chat via the generic object PATCH (quirk C9: `/chats/:cid` has no GET/PATCH; `PATCH /objects/:cid` works and shows in the re-list) — human titles are never overwritten, failures never fail the turn (`ChatTitler` policy in the transport, I/O in `anytype_chat_bot._maybe_title`). `default_mode` in `spaces.toml` sets the mode NEW chats start in (an unknown mode fails loudly at startup); persisted per-chat modes always win.

## Conventions

Follow **CLEAN** principles in all code: **C**ohesive (one reason to change per module), **L**oosely coupled (depend on ports/abstractions, not concretions), **E**ncapsulated (no reaching into internals — tests included), **A**ssertive (objects act on their own state rather than interrogating others'), **N**on-redundant (a rule or fact lives in exactly one place). The repo-specific rules below are applications of these:

1. **Business rules live in exactly one place** — edge legality in `GraphIndex.add_edge`, creation invariants in `schema.validate_new_node`, summary staleness in `NodeWriter`. Re-checking a rule in a second layer means it's in the wrong place.
2. **Domain stays pure**: no I/O, no `httpx`, no MCP types, no clocks — this keeps `tests/unit` in milliseconds.
3. Services take dependencies via constructor, typed against ports; a new tool ≈ a new application service shaped like `Explorer`.
4. Parameter objects mirror the tool surface (`ExploreQuery` ↔ the `explore` tool schema); keep them in lockstep.
5. Test names state behavior (`test_failed_link_rolls_back_the_created_node`), grouped in classes per scenario; fixtures build worlds through public APIs, never by poking internals.
