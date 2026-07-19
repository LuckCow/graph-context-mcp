# ADR 038: The turn's process trace lives on the intent node, carded on the reply

Date: 2026-07-17
Status: accepted

## Context

The Claude app shows a collapsible thought-process/tool-call view under
each reply. Our chat had pieces of that: WP19's live activity message
streams progress but DELETES itself once the reply lands; the turn diary
keeps everything but lives on the harness host, not in the space; intent
nodes (WP7) record provenance — but only for MUTATING turns, with a
condensed tool list, and their id was discarded at the end of the turn.
A pure Q&A turn that ran five tools left nothing a chat user could open.

The chat UI renders plain text (C7), so a "collapsible" view cannot be
inline; but a message can carry object-card attachments, and clicking a
card opens the object — the natural Anytype-shaped equivalent.

## Decision

**The intent node becomes the turn's background-process record, and the
reply carries it as an object card — on turns that did work.**

* **Intent nodes extend to working read-only turns.** This deliberately
  supersedes WP7's "read-only turns write nothing": `record_turn` now
  also records when a caller-rendered `process_trace` is present. "Work"
  = tool calls ran, the provider searched, or thinking was produced
  (real text now, thanks to ADR 037's `display: "summarized"`). A plain
  answer (no tools, no thinking, no mutations) still writes nothing —
  even when auto-capture minted an artifact, the reply stays clean.
* **The trace is complete at creation.** `orchestrator/process_trace.py`
  (`ProcessTrace`) folds each decision (thinking, interim text, calls,
  search digests) and each tool result beside the observer/turn-log
  taps; the pipeline passes `render()` into
  `IntentRecorder.record_turn(process_trace=...)` as a plain string —
  the application layer never imports the orchestrator, and the
  write-once-by-policy body rule (ADR 010) is untouched: no post-turn
  PATCH, no extra API request. In the body, `### gc:process` supersedes
  the old condensed `### gc:tool_trace`; `### gc:touched` renders
  `(none)` for read-only work, and such nodes link nothing.
* **`ReplyEvent.attach`** carries object ids a reply should present as
  cards independent of its text. `_finish_turn` returns the intent node
  (previously discarded); the pipeline stamps the last reply event when
  the turn worked. The Anytype transport merges `attach` ahead of
  text-scraped `object_references`, deduped, `MAX_ATTACHMENTS` cap,
  first chunk only (C8: the placeholder edit carries the cards).
  Discord/CLI/MCP ignore the field.
* **WP19 is unchanged.** The live activity message still streams and
  still deletes on close — it is ephemeral scaffolding; the intent card
  is the durable record. Renderers stay separate on purpose:
  `ActivityLog` fights a 2000-char chat budget and collapses;
  `ProcessTrace` is the archive (per-item soft caps, no collapse, under
  the 100k intent-body cap).

## Consequences

* Chat parity with the Claude app's disclosure UX, Anytype-style: tap
  the card on a reply to read exactly how it was made — thinking
  summaries, each tool call with args, each result, search digests.
* One more node write per WORKING read-only turn (mutating turns
  already wrote it); the card itself adds no request. ~1 req/s budget
  unaffected in the common chat case.
* Intent volume grows; they remain infra-role (hidden from traversal,
  never in the working set) and self-write-suppressed in resync.
* The subscription driver produces no thinking text today, so its
  thinking-only turns won't trace; tool-using turns trace on both
  drivers.
