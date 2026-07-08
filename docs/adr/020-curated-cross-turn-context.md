# ADR 020: Curated cross-turn context (scratchpad, working set, conversation memory)

Date: 2026-07-08
Status: accepted

## Context

The chat bot had no cross-turn memory by design: each turn's transcript
was the current user message alone, and every `decide()` was a fresh
stateless CLI session (WP6 deferred the problem to "the SDK's
session-resume machinery when dogfooding asks for it"). Dogfooding asked.
The model re-oriented from scratch every turn — re-exploring nodes it had
just written, losing stated intentions between messages.

Two earlier mechanisms pointed at the answer without reaching it. The
per-response `[project | focus | recent]` header was removed 2026-07-06
as token waste — its failure was *per-response* repetition, not the idea.
And the focus stack (implicit push-on-touch + a `pinned` flag) never
matched its actual intent: *a selected node whose full picture — body and
graph connections — guides further exploration*. Since the header's
removal, `RecentHistory` had zero readers and `pinned` only affected
eviction.

## Decision

Cross-turn context is **curated by the model, echoed once per turn**.
Three tiers, from deliberate to automatic, all persisted in the existing
`SessionContext` snapshot (now `version: 2`):

* **Scratchpad** — free text the model REPLACES wholesale via
  `context action="note"` (≤2000 chars; over-cap errors teach
  condensing). Intentions and open threads live here; durable facts
  belong in the graph. Notes flush to the store immediately, bypassing
  the mutation debounce — cross-turn memory must not be lost to a crash.
* **Working set** — the focus stack is retired. A `WorkingSet` holds
  nodes the model explicitly `hold`s at a granularity bucket: ≤2 at
  `full` (summary + body + one-hop edges every turn) and ≤6 at
  `summaries` (one-liner). Bucket membership IS the act of keeping;
  there is no pinned flag and tool activity never pushes into it.
  Overflow demotes the oldest full entry to summaries / evicts the
  oldest summary entry, reported in the tool response. Caps are domain
  rules in `WorkingSet`. `explore`/`find_path` default to the
  working-set top, falling back to the most recently touched node.
* **Recent history** — unchanged, automatic, now rendered again (a
  names-only trail line).

**The turn-start context block** (`interface/context_block.py`) renders
all three plus the project label as the first transcript event of every
orchestrator turn — assembled exactly once per turn; each `decide()`
re-renders the transcript so the block rides every decision free. A
session with nothing to say renders `""` and costs nothing. Over a
character budget (3500) it degrades in order: full-entry bodies (each
replaced by an explicit omission note), summary-entry edge lines, the
recent line; full-entry edge lines survive to the floor — the
connections of the node being worked from are the block's point.
Vanished nodes are skipped, never fatal. One-hop edges come straight
from `GraphIndex` adjacency (pure, no I/O); infra-role neighbors stay
hidden as everywhere else.

**Conversation memory** — the pipeline keeps a bounded per-session ring
(`ConversationMemory`, ~8 turns / 6000 chars) of prior user/assistant
messages, replayed ahead of the block each turn. `/clear` is a pipeline
command like `/mode` (every transport gets it) that empties the ring and
says what it kept. On the Anytype transport, `/clear` also records the
message's `order_id` in a second persisted `ChatCursor` (`clear_marks`);
startup catch-up seeds the ring from the already-answered slice of the
fetched window — after the watermark, at or below the cursor, bot
messages recognised by the same sent-ledger/identity signals the gate
uses, `/`-commands dropped. No chat messages are ever deleted:
"clearing" is a context boundary and the visible chat stays the human
record (there is no bulk-delete endpoint anyway). The watermark lives in
transport state, not `SessionState` — `order_id` is a chat quirk (C3).

## Consequences

* The model deliberately decides what to remember, with three costs made
  legible to it: scratchpad chars, bucket slots, memory turns.
* Snapshot v2 restores v1 leniently (focus entries become summary
  holds); the `SessionStore` port and both adapters are untouched — the
  snapshot dict just grew fields.
* `Detail` moved from `interface.presenters` to `domain.models` (the
  working set persists it); presenters re-exports it.
* The context tool surface changed: `focus`/`pin`/`unpin`/`remove` →
  `hold`/`release`; `note` added; `get` echoes the session. Docstrings
  re-pinned in all three profile goldens.
* Per-space session remains the unit (one SessionContext node per
  space); per-chat/thread context is WP8's keyed store, which this
  design slots into — the pipeline's memory, cursor, and clear marks are
  already keyed by chat id, and the snapshot is self-contained.

## Alternatives considered

* **SDK session-resume** (WP6's named lever) — opaque token growth,
  fragile across restarts, no scoped clear. Rejected.
* **Reply-parsing / end-of-turn auto-summarize** for the scratchpad —
  fragile extraction, or a doubled LLM bill. An explicit tool call is
  deterministic and teaches itself through its docstring.
* **Deleting chat messages on /clear** — destroys the human record and
  cannot reach past the ~100-message recency window (C2). A watermark
  clears context without touching history.
