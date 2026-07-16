# ADR 032: Chat files, to and from the model

Date: 2026-07-16
Status: accepted

## Context

Claude-app parity, third piece (WP23): drop a file into the chat and the
assistant can read it; ask for a document and the assistant can send one
back. Spike S13 mapped the previously-unspiked file surface of the local
API (quirk C10): `POST /v1/spaces/:sid/files` uploads (multipart,
MIME-sniffed, FLAT response), `GET /files/:id` serves the raw bytes with
a Content-Type header, the upload is a REAL object (`image`/`file` type,
size + extension properties, no MIME property), and chat messages
reference files through the same `{"target", "type"}` attachment
envelopes as C7 — **inbound messages expose their attachments**, we had
just been dropping them.

Two provider facts shape the model side: the Messages API takes images
as base64 content blocks, and the claude-agent-sdk's `query()` accepts a
message-dict form whose `content` can carry the same blocks into the CLI
(live-verified on subscription auth: the model described a test swatch's
color).

## Decision

**Inbound — classify from facts, fetch only what the model gets.** The
transport policy (`classify_attachment`, pure) decides from pre-download
facts (type key, size, extension): `image` type within 5 MB → fetch and
ride the user event as `ImageAttachment`s (name, exact MIME from the
download header — png/jpeg/gif/webp only, anything else degrades);
`file` type with a known text extension within 200 KB → fetch and FOLD
into the message text as a fenced `<file name="...">` block; everything
else → a one-line note (name + size + why), so the model knows the file
exists; an ordinary object card → named so the model can `find_node` it.
A bare file drop (empty text) is now a turn — the gate accepts
attachment-carrying messages. The composition root owns all I/O
(`_resolve_attachments`); a single unreadable attachment degrades to its
own note, never the turn.

**Images extend the transcript model, text extends nothing.**
`TranscriptEvent.images` carries inbound images on the user event —
turn-local like `thinking` (cross-turn memory keeps only text, so a
follow-up turn no longer sees the pixels). The API driver emits native
image blocks ahead of the text; the SDK driver switches `query()` to the
message-dict form (`query_payload`) — the rendered text transcript stays
the text half and notes each image by name. The turn diary redacts image
base64 to a size note (megabytes of pixels would evict everything else).
Text files need no model change at all: fenced text in the user message
is already every driver's native food.

**Outbound — a `send_file` tool queues; the transport delivers.** The
model calls `send_file(name, content)` (bound in every mode, like
`context`/`schedule` — delivery, not graph authorship; text formats
only, ≤200 K chars, ≤4 per turn, filename validated with path segments
stripped). The tool appends to a TURN-scoped `Services.outbox` — no I/O
in the tool — and the pipeline drains the queue into `file` reply events
after the reply text. The Anytype transport uploads each one and posts a
`📎 name` message carrying the file as a card (`type: "file"` envelope);
surfaces without a file primitive (Discord, the CLI, MCP) render the
content fenced under its name. The outbox clears at turn start, so a
crashed turn's leftovers never ride out with the next reply.

## Consequences

- The chat handles the Claude-app basics: drop a CSV and ask about it,
  paste a screenshot, ask for an export and get a real file back.
- Caps are module constants (5 MB image / 200 KB text in, 200 K chars
  out) — tunable without design changes; oversized files degrade to
  informative notes instead of blowing the context window.
- Images are turn-local: "what about the left side?" one turn later
  re-requires the image. Acceptable v1; persisting images across turns
  would ride the same WP22-style event replay if dogfooding wants it.
- PDFs and binaries surface as name+size stubs for now; the API driver
  could take PDFs as document blocks in a follow-up (the SDK path has no
  equivalent, so behavior would differ by driver — deferred).
- Quirk C10 joins the quarantine (`chat.py` header), the mock models the
  full surface (multipart parse included), and the live E2E pins
  upload → attach → inbound exposure → byte-faithful download.
