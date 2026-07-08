# ADR 021: Per-chat keyed sessions

Date: 2026-07-08
Status: accepted

## Context

WP15 gave each session a curated cross-turn context — scratchpad, working
set, conversation memory, mode. But everything in the domain `SessionState`
was a **per-space singleton**: one `SessionState` per runtime, persisted to
one unkeyed `gc_session_context` node. That singleton was the sole reason
for the one-chat-per-space invariant (ADR 017/019: "two runtimes would
clobber each other's snapshot"), and it meant a space could only ever be
one working context.

The goal (WP8, scoped to threads): multiple chats inside one space, each a
**stable thread** with its own scratchpad, working set, and mode — so a
user can work on different story arcs or project aspects side by side with
organizational hygiene, and creating a chat in Anytype creates a new
thread with no config change.

The ground was already laid: session ids (`anytype:<chat_id>` /
`discord:<channel_id>`) flow into `handle_message`; the pipeline's mode +
`ConversationMemory`, the chat cursor, and the `/clear` watermarks were
all already keyed by chat; the SSE loop is one task per chat. The only
un-keyed thing was the domain `SessionState` and its persistence.

## Decision

**Sessions are keyed by an explicit transport-scoped id, everywhere. There
is no unkeyed or default session.**

- **Keyed store.** `SessionStore.load(key)` / `save(snapshot, key)` — the
  key is required; an empty key raises `ValueError` (a bug, never data).
  Each key owns one `gc_session_context` node, discriminated by a new
  `gc_session_key` text property; the Anytype adapter finds a session by
  type-scoped search + client-side exact key match. Keys seen in the wild:
  `"mcp"`, `"cli"`, `"anytype:<chat_id>"`, `"discord:<channel_id>"`.
- **`SessionRegistry`** is the one place live sessions come from: lazy
  `load_or_fresh` per key (with a lock so concurrent first-turns resolve
  to one object), cached for the process, `flush_all()` at teardown.
  `composition.build_runtime` builds the registry and exposes
  `services_for(key)` — a per-key `Services` view (its own `SessionState`,
  the runtime's shared repository/journal/capture/projector/ranker via
  `derive_services`). The MCP server binds its primary bundle to key
  `"mcp"`; orchestrator paths get an inert donor bundle and route
  everything through `services_for`.
- **Mode is persisted per chat.** `SessionState.mode` (an opaque label,
  like `project`) is the persisted mirror of the pipeline's authoritative
  in-memory mode; restored on a session's first turn when it names a
  loaded spec, written back on `/mode` with an immediate flush (a store
  outage degrades to an in-memory-only switch + notice, never un-switches
  or fails the turn).
- **Serve all chats, minus an exclude list.** The bot enumerates a bound
  space's chats and serves each (`served_chat_ids`: all listed minus
  `exclude_chats`, or a single pinned `chat_id`). All chats of one space
  share **one runtime and one `ChannelRoute` lock** — turns serialize per
  space, keeping the shared journal/capture in `_finish_turn` correct
  (ADR 009's write lock already covers the store layer). New chats become
  new threads with zero config.
- **Live discovery.** A per-space watcher re-lists chats every
  `GC_CHAT_RESCAN_SECONDS` (default 60; `off` disables) and registers +
  serves any new one without a restart. `main()` uses an `asyncio.TaskGroup`
  so watchers spawn serve tasks into the bot's lifecycle. The routing maps
  are shared (aliased) between `SpaceRuntimes` and the turn handler, so a
  `register_chat` addition is live immediately.

## Consequences

- One space is now N independent working contexts. The one-chat-per-space
  invariant is lifted; the remaining reason two *runtimes* can't share a
  space is duplicated repositories/journals, not the session node.
- **No legacy compatibility** (deliberate): pre-WP8 unkeyed
  `gc_session_context` nodes match no key and are inert — the adapter warns
  once naming them. The two dogfood spaces convert by hand (copy the
  `gc_fields` JSON into the keyed node in the Anytype UI) only if the old
  scratchpad matters; otherwise delete them. No adoption logic, no `""`
  sentinel — an explicit key is the single rule.
- Discord and the CLI get keyed sessions for free (each channel/CLI mints
  its own node in its bound space); no transport changes.
- `asyncio.TaskGroup` tightens the bot's crash semantics — any task that
  raises cancels the group. The serve and watch loops are designed never
  to raise (errors degrade into their backoff/retry paths), so this only
  hardens shutdown. A deleted/archived chat's serve task is left running;
  its stream errors into the existing capped backoff (accepted noise).

## Alternatives considered

- **A `""` default key / keep the unkeyed node for back-compat** — the
  user flagged the sentinel as a bug magnet, and there is no deployment to
  preserve. An explicit required key is simpler to reason about and makes
  "which session?" un-guessable-by-accident.
- **A tool/`switch_space` parameter** (ADR 017 already rejected this) —
  drags a transport concern through every layer; the runtime bundle is the
  isolation unit.
- **Per-chat runtimes** (one repository/journal each) — wasteful: chats in
  one space share the same graph; only the session differs. One runtime,
  N session views is the minimal split.
