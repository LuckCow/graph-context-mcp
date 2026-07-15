# ADR 019: Anytype in-space chat as a transport; headless CLI sidecar

Date: 2026-07-07
Status: accepted (transport shipped; sidecar prepared, cutover deferred).
Amended by ADR 029 (WP19): the "Processing…" placeholder is now claimed
as a live activity message while the turn streams and the reply posts as
a fresh message; a mode whose `activity_detail` is `off` keeps this
ADR's original edit-into-reply lifecycle.

## Context

The orchestrator chatted over Discord while the knowledge graph lived in
Anytype — replies could not link the objects they talked about, and the
whole stack depended on the desktop app running on the host, reached
through the devcontainer firewall at `host.docker.internal:31009`.

Two upstream changes removed the constraints. Anytype's local API gained
a Chat API (heart v0.50.7, under the already-pinned `2025-11-08` version
header): chats/messages CRUD and an SSE stream — live-confirmed against
the desktop endpoint (spike S10). And `anytype-cli` runs anytype-heart
headless with **bot accounts** (`anytype auth create`), headless API-key
minting (no desktop pairing), invite-based space joins, its own outbound
sync, and a disableable server rate limit.

## Decision

**Anytype's own in-space chat is a first-class transport, and the bot's
Anytype node will be a headless CLI sidecar.** Discord remains supported.

Transport shape (mirrors the Discord quarantine):

* `orchestrator/spaces.py` — `spaces.toml` bindings keyed by the space id
  itself (`[spaces."bafyre..."]`, optional `profile`/`project`/
  `modes_file`/`chat_id`). One-binding-per-space is *structural*: TOML
  rejects duplicate table keys. `chat_id` unset means discovery — fails
  loudly unless the space has exactly one chat.
* `orchestrator/anytype_chat_transport.py` — plain-logic policy (no
  infrastructure imports; import-linter enforced): gate, identity
  (`session_id=anytype:<chat_id>`, `user_id=anytype:<member_id>`,
  ADR 008's `<transport>:<id>` convention), deep-link rewriting,
  chunking. `orchestrator/rendering.py` holds `render`/`chunk`,
  extracted from the Discord module and re-exported there.
* `orchestrator/anytype_chat_bot.py` — the composition root: per-space
  transport clients (separate from each runtime's repository client),
  startup catch-up, one SSE serve task per chat, jittered capped
  reconnect backoff. The SSE read timeout is tied to the heartbeat
  (2x + margin) so a half-dead stream raises instead of hanging.
* `infrastructure/anytype/chat.py` — the chat quirk quarantine (C1–C6,
  the chat analogue of `mapping.py`'s A-series), pinned by spike S10 and
  mirrored by `MockAnytype`'s chat routes.

Behavioral contracts:

* **Echo suppression is belt and suspenders.** Every id returned by the
  bot's own message POSTs lands in a bounded `SentMessages` ledger,
  **persisted next to the cursor** — live testing caught a restart
  answering its own previous-life reply during catch-up, which nothing
  else can prevent on the desktop endpoint where bot and human share one
  account. Once the sidecar's bot account exists,
  `creator == bot_member_id` is dropped too (there is no "who am I"
  endpoint today — quirk C6).
* **The chat cursor persists** (`GC_CHAT_CURSOR`, default
  `logs/chat_cursor.json`) so *messages sent while the bot was down are
  answered at the next startup* (bounded by the messages endpoint's
  ~100-message recency window). Only a chat with **no** persisted
  position fast-forwards past its history — a freshly bound chat must
  not run a turn per historical message. Losing the file degrades to
  exactly that first-run behavior; the cursor advances *before* the
  turn runs, so a failing turn cannot loop.
* **Intent nodes point at their triggering message**: `handle_message`
  and `IntentRecorder.record_turn` carry an `origin` field
  (`anytype:<chat_id>:<message_id>`) — the "which conversation moment
  caused this" half of attribution, next to `gc_user_id`'s "who".
* **Replies attach the graph** (amended after live dogfooding): the
  chat UI renders message text as PLAIN TEXT — markdown shows its
  literal glyphs, so text links cannot be links (quirk C7). Replies are
  therefore `plainify`d (markdown stripped, `[Name](bafy…)` collapses to
  the name) and every referenced object id rides the first chunk as a
  message **attachment** (`{"target", "type": "link"}` envelopes — a
  bare id list 400s), which clients render as clickable object cards —
  a better surface than inline links anyway. The original deep-link
  rewriting (`linkify`) was replaced by `object_references` +
  `plainify` the day after shipping.

Sidecar topology (initially behind an opt-in compose profile; the gate
was dropped 2026-07-07 when cutover began, so the service now starts
with every up/rebuild): a second compose service builds `anytype-cli` and serves the
HTTP API on `0.0.0.0:31012` with `ANYTYPE_API_DISABLE_RATE_LIMIT=1`. It
lives *outside* the devcontainer's egress firewall — its outbound sync to
the Anytype network is its whole purpose — while the dev container
reaches it over the compose subnet (already allowed). Everything is
endpoint-agnostic via `ANYTYPE_BASE_URL`/`ANYTYPE_API_BASE_URL`, so the
cutover is: create the bot account + API key in the sidecar, invite the
bot to each space, flip the base URL to `http://anytype:31012`, and add a
`depends_on: service_healthy`. Until then the transport runs against the
desktop endpoint, posting as the user's own account.

## Consequences

* Humans keep the desktop app as their editing (and now chatting)
  surface — ADR 001's premise is *strengthened*: chat, graph, and
  provenance share one store, and replies link into it.
* One space must be bound by **either** the Discord bot **or** the
  Anytype chat bot, never both (a space holds one SessionContext node;
  LWW clobbering). Operator invariant — stated in both TOML headers —
  not machine-checkable across processes.
* Offline catch-up is bounded by the messages recency window (~100);
  a longer outage drops the excess silently. Acceptable for a personal
  assistant; revisit if it bites.
* Chat replies are writes to the same space as graph writes; against a
  throttled server they contend for the same ~1 write/s budget. The
  sidecar removes the server cap; ADR 009's single-writer pacing and the
  client's 429 backoff stay as defense in depth.
* The mock's SSE stream is an approximation; `tests/e2e/test_live_chat.py`
  pins the same behaviors against a real server (fakes-are-contracts).
