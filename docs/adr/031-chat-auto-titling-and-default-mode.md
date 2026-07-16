# ADR 031: Chat auto-titling and the per-space default mode

Date: 2026-07-16
Status: accepted

## Context

Claude-app parity, second half (WP21). In the Claude app a new
conversation names itself after the first exchange; in our Anytype bot a
chat keeps whatever name it was born with — usually nothing. And every
new chat starts in the profile's `default_mode`, which for the fiction
profile is `world_modeling`; a deployment whose chats are mostly Q&A
wants to choose a different starting mode without forking the profile.

Renaming a chat was UNPROVEN before this work: the chat API surface we
had spiked (S10, quirks C1–C8) covered create/list chats, message CRUD,
and SSE — no endpoint had ever updated a chat *object*. Spike S12
settled it (quirk C9): `PATCH /v1/spaces/:sid/chats/:cid` does not exist
(404, as does the single-chat GET), but the chat IS addressable through
the **generic object route** — `PATCH /objects/:cid {"name": ...}`
returns 200 and the next `/chats` re-list reflects the new name. A chat
created without a name is born with `name: ""`.

## Decision

**Harness-generated titles, once per chat, after the first real
exchange.** The transport policy (`ChatTitler`,
`anytype_chat_transport.py`) is pure: an "untitled" test over the
runtime's live chat-name registry (`""`, `"Chat"`, `"New chat"` read as
untitled — anything else is a human's title and is never overwritten),
a one-attempt-per-process guard (no persistence needed: the name check
re-derives the state after a restart), a one-shot titling prompt, and
defensive sanitization (first line, wrappers stripped, ≤60 chars).

The composition root (`anytype_chat_bot._maybe_title`) owns the I/O:
after a successful non-command turn in an untitled chat, ONE side-call
through the existing driver abstraction
(`route.orchestrator.driver.decide` with a tiny transcript, no tools)
generates the title, and ONE `rename` PATCH (the new C9 client wrapper,
`update_object` underneath) applies it. This runs after the reply is
already delivered — off the user-visible path — and any failure logs a
warning, never fails the turn. Error/notice-only turns do not consume
the attempt; the next real exchange retries.

Going through the driver abstraction means titling works on the
subscription driver with no API key. The cost on that path is one extra
CLI session per chat lifetime (seconds, quota) — acceptable because it
is once-per-chat and asynchronous to the reply. The rejected cheap
alternative (truncate the first user message verbatim) is the documented
fallback if the side-call misbehaves in practice.

The rescan watcher (`_watch_chats`) refreshes the shared name registry
for already-served chats, so a human's rename made in the Anytype UI
reaches the untitled test within one poll interval.

**Default mode is a `spaces.toml` per-space key.** `default_mode`
(sibling of `profile`/`modes_file`, ADR 019's config surface) overrides
the profile's `default_mode` when the registry loads. Sessions that
already persisted a mode keep it — the pipeline consults
`registry.default` only when nothing was persisted, which is exactly
"new chats start here". A `default_mode` naming a mode that is not
loaded fails LOUDLY at startup (deployment config is not code; unlike
the profile's own default, it does not silently degrade); the override
is re-applied by the `/mode` reload closure, so it survives registry
refreshes. An Anytype settings object was rejected: new machinery for
one scalar, and mode *selection* is deployment config while mode
*content* is space content.

## Consequences

- New chats behave like the Claude app: ask a question, the thread
  names itself; the chat list stays navigable without human effort.
- Budget: one extra decide + one PATCH per chat lifetime — noise next
  to the ~1 req/s API budget (ADR 029).
- Quirk C9 joins the quarantine (`chat.py` header), the mock models it
  (generic-object GET/PATCH serve chats), and the live E2E round-trip
  pins rename + re-list against the real server.
- A human can pre-title a chat to opt out of auto-titling entirely, and
  `exclude_chats`/mode config keep working unchanged.
