# ADR 036: Chat text formatting via marks

Date: 2026-07-17
Status: accepted; amended same day — the API-driver citation capture
sketched below SHIPPED (see the amendment at the bottom)

## Context

The Anytype chat UI renders message text as PLAIN TEXT (quirk C7): the
markdown the model writes shows its literal glyphs, so a reply citing
`[the API docs](https://developers.anytype.io)` — or the web-search
sources list ADR 030 turns surface — arrives as unclickable noise. The
Chat API's cure is the `marks` array a message accepts beside `text`:
`{"from", "to", "type"}` ranges (plus `"param"` for link targets) over
the message text, mark types `bold | italic | underscored |
strikethrough | keyboard | link | object | mention | emoji | text_color
| background_color`.

Spike S14 (live sidecar, API `2025-11-08`) pinned the wire behavior as
quirk **C11** before any code was written:

* Marks land on create AND edit, round-trip verbatim at
  `content.marks`, and are part of C8's wholesale edit replacement — a
  PATCH without `marks` drops them.
* **Offsets are UTF-16 code units, not code points.** On a text of 6
  code points / 7 UTF-16 units (an emoji up front), `to=7` is accepted
  and `to=8` is rejected — the bounds check names the unit.
* A range that is negative, inverted, or past the text's UTF-16 length
  fails the whole POST with a **500** — which our client retries
  through its full backoff ladder before raising, so a bad mark does
  not fail fast; it stalls the turn and strands the reply.
* Unknown mark types and param-less links are accepted silently; a
  non-list `marks` 400s (Go unmarshal error).

## Decision

**Convert the model's markdown to plain text + marks at the chat
client, in one place, correct by construction.** The model keeps
writing what it already writes — markdown — and no other layer changes:

* A pure converter (`infrastructure/anytype/marks.py`,
  `to_marked_text`) handles the INLINE subset: `[label](url)` links,
  bare URLs (trailing sentence punctuation and unbalanced parens
  shed), `**bold**`, `*italic*`, `~~strikethrough~~`, and `` `code` ``
  (→ `keyboard`). Nesting recurses (`**[label](url)**` yields both
  marks). Block syntax stays literal text, and fenced code blocks
  shield their contents from inline parsing. Anything unmatched or
  malformed degrades to literal text — what the chat shows today is
  always a safe floor, a bad range is not.
* Because an invalid range 500s (retried!), the converter both builds
  offsets in UTF-16 units as it emits and bounds-filters every mark
  against the final text before returning. No mark leaves the module
  unvalidated.
* `AnytypeChatClient._message_body` applies the conversion to EVERY
  outbound message — replies, activity-trace edits (WP19), file-card
  captions (WP23) — so send and C8's wholesale edit stay symmetric.
* The mock mirrors C11 (storage at `content.marks`, absent-key
  semantics, the 400/500 validation split); the live E2E round-trips
  an emoji-prefixed link, which doubles as a live UTF-16 bounds check.

Underscore emphasis (`_em_`) is deliberately NOT parsed — `gc_session_key`
and friends appear in chat text constantly — and unknown-to-us syntax
never errors, it just stays visible.

**Inline web-search citations ride the same path.** The subscription
driver's SDK exposes reply text only (no per-span citation metadata),
so structured citation placement cannot work there; but the model
already holds every search result's URL (WP22 replay), so mode prompts
can simply ask for inline markdown links — the converter makes them
clickable on every driver. The Messages API does return span-accurate
`citations` on text blocks (currently dropped by `turn_from_response`);
capturing those is a possible API-driver-only refinement, not part of
this ADR.

## Consequences

* Links in replies (and sources lists) are clickable in the Anytype
  clients; bold/italic/strike/code render styled instead of as glyphs.
* The stored chat text no longer equals the model's raw reply
  (markdown syntax is consumed). Echo suppression is unaffected — it
  keys on identity and posted-message ids, never text — and
  `ConversationMemory` replays the model-side transcript, not the chat
  store.
* Inbound is untouched: `to_chat_message` still reads `content.text`
  and ignores marks, so a human's link mark whose text is not the URL
  reaches the model without its target. Acceptable today (humans paste
  bare URLs); reconstructing markdown from inbound marks is a
  follow-up if it ever bites.
* Headings and list markers still show literally — there are no block
  marks to map them to.

## Amendment (2026-07-17): API-driver inline citations

With the deployment preparing to switch its default driver to
`anthropic_api`, the refinement above shipped. With citations enabled
(web search), the Messages API splits the reply into a **text block per
cited span** and hangs `citations` (`web_search_result_location`: url,
title, cited_text) on the block — the block boundary IS the placement
information. `turn_from_response` now:

* folds each cited block's sources in as inline markdown links appended
  right after the span — ` ([domain](url))`, deduped by URL, parens in
  URLs percent-escaped so the markdown target survives the marks
  converter — which every surface renders natively and the Anytype
  transport makes clickable;
* concatenates ADJACENT text blocks verbatim instead of joining all
  text blocks with `\n\n` — citation splits land mid-sentence, and the
  old join shattered cited paragraphs (a latent WP20 bug, fixed here).
  Text separated by other block kinds keeps its paragraph break.

The subscription driver stays as-is by decision: its SDK exposes reply
text only, so there is nothing to capture there. Modes that want cited
prose on that driver rely on prompting the model to write inline links
(the WP22 replay keeps every URL in its context).
