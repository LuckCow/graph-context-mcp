# ADR 041: Schema proposals — LLM-drafted, reaction-confirmed type changes

Date: 2026-07-19 (v2 same day: confirmation moved from conversational
interpretation to a mechanical 👍-reaction path)
Status: accepted (amends ADR 006's "the server never invents vocabulary"
with ONE explicitly human-gated path)

## Context

ADR 006's space-reflecting model made the user's existing Anytype
types the node vocabulary and deliberately gave the server no way to
create or modify types: humans author schema in the Anytype UI, the
server reflects it. That kept ownership clean but left a gap in
practice: "we should track factions" dies in conversation, because the
assistant can neither act on it nor even hand the user a concrete
draft. The nearest precedents — `create_missing_relations` (ADR 006)
and `create_missing_fields` (ADR 023) — already let the model mint
*space-level* vocabulary behind an explicit per-call opt-in, but
neither can create a TYPE, nor attach anything to a type (a
`POST /properties` mint is space-level only), and their opt-in is a
flag the model itself sets, which is too weak a gesture for reshaping
the space's schema.

The first cut of this ADR let the model call `apply` after the user
confirmed in conversation, with a turn-watermark gate enforcing only
*sequencing* (propose and apply could not share a turn). That left
consent interpretation to the model — a norm, not a guarantee. v2
removes the model from the loop entirely, enabled by spike S15
(quirk C12): the chat API exposes message reactions — a toggle route,
account identities per emoji, and a live `reactions_updated` SSE frame.

Three live-server quirks constrain the implementation:

* **A11** (spike_type_update): `PATCH /types/:id` with a `properties`
  list replaces the type's fields WHOLESALE — a careless update strips
  a human's fields.
* **A12**: a property's format is immutable; the only migration is
  delete + re-create.
* **C12** (spike S15, 2026-07-19 live sidecar): `POST
  .../messages/:id/reactions {"emoji": "👍"}` toggles the calling
  account's reaction; a message's `reactions` is `{"<emoji>":
  ["<account identity>", ...]}`; toggling emits an SSE
  `reactions_updated` frame whose payload is `{"id", "reactions"}`
  bare — no message envelope — and reaction frames are NOT replayed
  with the connect-time backlog.

## Decision

**The model drafts; only a human's reaction executes.** The eleventh
tool, `schema`, lets the LLM *draft* a schema change — a new type
(`propose_type`) or new scalar properties on an existing type
(`propose_fields`) — into a session-scoped proposal ledger
(`application/schema_proposals.py`; also `list` and `cancel`). The
tool has **no apply action** — an attempted `apply` is an error that
explains the contract. That absence is the guarantee: there is no
sequence of tool calls by which the model can change the schema.

**The confirmation message is harness-authored.** Drafts ride out of
the turn as `confirm` reply events (drained after the reply like the
WP23 outbox; text rendered from the STORED proposal, so the human
always confirms the exact change, never the model's paraphrase). The
Anytype transport posts each as its OWN message — never the reply
placeholder, so a reaction on it is unambiguous — appends the
instruction line ("React 👍 to APPLY … 👎 to dismiss"), and arms a
watch on the posted message id.

**The reaction handler applies with no model turn.** The bot routes
`reactions_updated` frames (and, because C12 frames are not replayed,
a re-list sweep of tracked messages on every stream reconnect) into
`AnytypeChatTurnHandler.handle_reaction`: a 👍 from any non-bot
account identity applies the proposal through the repository under the
route lock; 👎 discards it; the bot's own identity never counts (on
the desktop endpoint, where bot and human share an account, every
identity counts — the bot never reacts). The outcome posts as a plain
message ("✅ applied p1: created type 'Faction' …"); a failed apply
posts the error and keeps the watch armed. Surfaces without a
reaction channel (Discord, the CLI) render the confirm event with a
where-to-confirm note; the bare MCP server can draft and cancel but
never apply.

**Proposals are drafts, not records.** Session-scoped, in-memory,
capped at 5 pending; the reaction watch is in-memory beside them, so a
restart clears both together and a stale 👍 is silently inert (the
handler answers a half-stale one with "no longer pending"). The
applied change needs no proposal record — the minted type and
properties ARE the durable, human-visible outcome.

**Two new port methods, contract-tested on both backends.**
`GraphRepository.create_type(name, plural=, properties=)` and
`.add_type_properties(type_identifier, properties)`, taking domain
`PropertyDraft` values (name, `FIELD_FORMATS` format, select options)
that validate at construction. Semantics both backends share:

* The created type is usable by `create_node` immediately — the
  Anytype adapter registers the response in its live `SpaceRegistry`
  (`register_type`, the type-level `register_property`), no resync.
* A draft whose name matches an existing space property is REUSED
  (attached) when formats agree; a format mismatch is a
  `SchemaChangeConflict` (A12 — never mint a shadow), as is a name
  that matches an existing relation (an edge, ADR 006) or an existing
  type (for `create_type`).
* A draft already on the type with a matching format is a no-op, so a
  confirmed proposal survives a retry.
* `add_type_properties` is A11-safe by construction: the adapter
  re-fetches the type inside the single-writer critical section and
  resends the full property list plus additions, the same discipline
  as bootstrap's retrofit. Existing fields survive — this is the
  contract test's core assertion.
* Select options seed find-or-create as tags (create-only; human
  renames/recolors survive), mirroring bootstrap.

**Scalars only.** Proposed properties take the ADR 023 `FIELD_FORMATS`
vocabulary; `objects`-format relations stay on their existing path
(`links` + `create_missing_relations`). Renames, deletions, and format
changes are out of scope — those stay human-only, in the Anytype UI.

**Bound in every mode**, like `schedule` and `automation`: with no
model-side apply, binding the drafting surface everywhere lets any
mode carry the conversation from idea to confirmed draft while the
human alone authorizes execution.

## Consequences

* "Add a Faction type with Motto and Alignment" is one exchange: the
  model drafts, the harness posts the exact change, the user taps 👍,
  the harness applies — the model cannot execute a schema change even
  by misbehaving, and never has to interpret consent.
* ADR 006's reflection stance survives amended: every schema write
  traces to a human's reaction on an exact rendered diff.
* The confirm-message + reaction-watch pattern (`pending_confirms`,
  `handle_reaction`, the reconnect sweep) is reusable for any future
  human-gated action (bulk deletes, merges); quirk C12 is now pinned
  in `chat.py`/`mock_server.py` for whoever needs it next.
* A reaction made while the bot is down is lost with the in-memory
  ledger (restart clears proposals and watches together); the user
  re-asks. Accepted for drafts.
* Types accumulate: the local API cannot delete types, so a
  wrongly-approved type is archive-in-UI only. The exact-diff confirm
  is the mitigation.
