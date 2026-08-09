# ADR 043: Activity-capped chat streams — hibernate + wake

Date: 2026-07-21
Status: accepted (amends WP8's serve-every-chat and ADR 019's startup
catch-up: both promises survive, but a live SSE stream is no longer how
every chat gets them)

## Context

The bot opens one SSE stream per served chat (`_serve_chat`), and each
stream holds one HTTP connection for its lifetime. All of a space's
streams share the space client's single `httpx.AsyncClient` — default
pool limit 100 connections — with every *request* the transport makes:
replies, activity edits, the rescan re-list, uploads. Chats accumulate
and never go away (the local API cannot delete them, and nothing prunes
served chats), so a long-lived space walks toward ~90+ streams, at which
point ordinary requests silently queue behind stream connections. The
failure mode is not an error but a mysteriously slow bot — the worst
kind.

Per-chat cost is otherwise trivial (an asyncio task, a socket, a
`: heartbeat` comment every 30s). The problem is purely that streams
pin pooled connections *forever*, and the count only grows.

A calendar cutoff ("only stream chats active in the past week") was
considered and rejected: a chat revived after the window would be deaf
permanently — a message into it would never be answered.

A live probe (2026-07-21, sidecar) settled the wake mechanism: the
`/chats` list rides the generic object shape, and the server maintains a
`last_message_date` date property on every chat with messages, advancing
on each post (bot posts included) — quirk **C13**, pinned in `chat.py`
and `MockAnytype`. (`GET .../messages?limit=1` returned nothing on the
probe, so per-chat message polling was not even a reliable alternative;
the C2 window endpoint is the only message read.)

## Decision

**Live streams are a capped, activity-ranked resource per space.** The
`GC_CHAT_STREAM_CAP` (default 20) most recently active chats — ranked on
`last_message_date`, an opaque orderable string, ties broken by chat id
— hold serve tasks; every other served chat *hibernates*: registered,
routed, sessioned, able to receive scheduled-event firings and replies
(posting needs no subscription), but holding no stream.

**The wake is the rescan poll.** `_watch_chats` already re-lists the
space's chats every `GC_CHAT_RESCAN_SECONDS` (default 3s) for
discovery; the same listing now feeds `plan_streams`
(`anytype_chat_transport`, pure): a message into a hibernated chat makes
it the newest, the next tick starts its serve task, and the cursor's
catch-up answers the message — answered late by at most one rescan
interval plus catch-up, never lost. The displaced stream is the least
recently active chat, which wakes back the same way. No extra requests,
no calendar, no deadline.

**Stops never abort work.** The serve task marks its chat busy
(`_SpaceStreams.busy`) around catch-up and each event it handles; the
watcher also treats chats holding a pending schema confirm (WP33 — the
👍 only arrives over that chat's SSE stream) as busy. The plan is
computed and applied with no `await` between the busy check and
`task.cancel()`, so single-threaded asyncio makes eviction race-free: a
cancel only ever lands while the task is parked on the stream read. A
busy survivor leaves the space transiently over cap; the tick after it
goes quiet retries.

**ADR 019's catch-up debt is paid stream-less.** At startup the roster
picks the top-N by the same ranking (chats with offline backlog are by
definition the most recent, so they get streams and catch up normally);
chats hibernated at startup are queued on `_SpaceStreams.catch_up` and
the watcher runs one plain `_catch_up` for each before its first tick —
offline backlog answered, first-run history fast-forwarded — retrying
failures per tick, and dropping any chat the roster wakes first (its
serve task catches up instead).

**The cap requires the watcher.** The rescan poll is the only wake
mechanism, so `GC_CHAT_RESCAN_SECONDS=off` loudly ignores the cap and
streams everything (`0`/`off` on the cap itself does the same by
choice). Pinned bindings (`chat_id`) serve exactly one chat and are
never capped.

## Consequences

* Steady-state connections per space: ≤ cap + the request in flight —
  the pool cliff is unreachable at any chat count.
* A hibernated chat's user sees a reply a few seconds later than a
  streamed chat's on the *first* message only; the woken stream then
  serves the conversation live (and its own reply re-ranks it newest,
  keeping it streamed).
* Reactions do NOT advance `last_message_date`, so a 👍 in a hibernated
  chat cannot wake it — that is exactly why pending-confirm chats are
  unevictable while the confirm is armed. (Confirms do not survive
  restart, ADR 041, so a hibernated chat can never hold one.)
* `AnytypeChatClient.list_chats` returns `ChatSummary` records
  (id/name/`last_message_date`) instead of `(id, name)` pairs; the
  bootstrap lister contract stays plain tuples (the composition root
  converts), keeping `bootstrap` infrastructure-free.
* The roster is memoryless — each tick recomputes top-N from the
  listing — so restarts, reconnects, and missed ticks need no repair
  logic.
* `serve.py` and Discord are untouched; the cap is an Anytype-transport
  concern.
