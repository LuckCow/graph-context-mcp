# ADR 010: Node descriptions live in the object body, fetched on demand

**Status:** Accepted (2026-07-02) — supersedes WP1's settled decision
"scalar fields are properties, never body; body is reserved for Prose
text"; corrects spike S6 / mapping assumption A6

## Context

A node's long-form description is stored in a `gc_description` text
property. Properties render as cramped side-panel fields in the Anytype
UI; the **body** is Anytype's primary editing surface — a full markdown
editor. Anytype exists in this system precisely to be the human editing
surface (ADR 001), so the long-form content humans actually revise was
living in the worst place to revise it.

The blocker was spike S6 (2026-06-21): "PATCH of `body` is silently
ignored" — recorded as *bodies are write-once*, which drove the
render-once Prose design and kept the mutable description as a property.

A re-spike on 2026-07-02 (live server, same pinned
`Anytype-Version: 2025-11-08`) plus a documentation check corrected this:

- **Body patching is a documented feature of `2025-11-08`** — that very
  version introduced "update an object's markdown body via
  `UpdateObjectRequest`"; before it, PATCH could not touch the body at
  all. `2025-11-08` is still the current API version (later server
  additions — chat endpoints, file upload/download/delete — were rolled
  into it as non-breaking additions), so no version bump is needed or
  available.
- The field names differ between create and update — a documented
  gotcha: create takes `body`, update takes `markdown`, for the same
  content. `PATCH {"body": ...}` is silently ignored (HTTP 200, content
  unchanged; re-confirmed live), while `PATCH {"markdown": ...}`
  replaces the body. The original S6 spike exercised only the `body`
  key and so recorded the absence of a feature that was present under
  the other name.
- The documented editing pattern is **GET → modify → PATCH the whole
  text back**: the single-object `GET` response includes `markdown`,
  and the PATCH is a full replacement, not an incremental edit.
- Neither `GET /objects` lists nor `POST /search` results carry
  `markdown`; bodies exist only on single-object `GET`. Bodies therefore
  *cannot* ride the hydrate sweep — on-demand retrieval is forced by the
  API, not merely preferred.

That last point aligns with ADR 002's discipline: the `GraphIndex` is a
light, derived projection. Descriptions were the heaviest thing in it,
and the API is telling us they never belonged there.

## Decision

1. **A node's description IS its Anytype body.** `gc_description` is
   retired from the write path. `Node` loses its `description` field;
   the index carries names, summaries, scalars, and edges only.
   `NodeDraft`'s separate `description` and `body` fields merge into one
   long-form field (the tool surface keeps the `description` parameter
   name — docstrings are prompts, and "description" reads better to the
   model than "body").
2. **Retrieval is on-demand via `fetch_body`.** `get_node` always
   fetches the node's body (one extra `GET`; reads are unthrottled, S7).
   `explore` with `detail=full` fans out `fetch_body` over its hits so
   scene assembly can still pull full text in one call. The fan-out's
   options and budgets are **explicitly provisional** — tune after
   dogfooding how the agent LLM actually uses it (cap the fetch count,
   make it a flag, or demote `full` to summaries-plus-fields if it
   proves too heavy).
3. **The body write asymmetry is a new quarantined quirk (A7):** create
   writes the `body` key; update PATCHes the `markdown` key (wholesale
   replace); `body` in a PATCH is silently ignored. `MockAnytype` pins
   all three behaviors **and** stops returning `markdown` from
   list/search handlers (it currently does, which would mask hydration
   code accidentally depending on it).
4. **Transition fallback lives in `fetch_body`:** return `markdown` if
   non-empty, else the object's `gc_description` property (the
   single-object `GET` carries properties, so this costs nothing). A
   one-shot migration script moves existing `gc_description` values into
   bodies and clears the property; after migration the fallback is dead
   code to delete.
5. **Write-once bodies become policy, not constraint.** Prose and intent
   nodes (ADR 008) keep immutable bodies because provenance should not
   be editable — documented as a rule we chose, no longer a limitation
   we inherited.

## Consequences

- The Anytype UI becomes a first-class place to write descriptions, and
  because every read is a fresh fetch, a human's body edit is visible to
  the very next `get_node` — *fresher* than the property was, which sat
  stale in the index until a resync.
- `get_node` costs one extra unthrottled `GET`; `explore full` costs one
  per hit, bounded by the traversal's own caps. This is the first
  tuning candidate (see WP9 open questions).
- Body PATCH is wholesale replace: human-vs-bot description conflicts
  are last-write-wins, consistent with the WP1 stance; bot-vs-bot is
  already serialized by the ADR 009 single-writer seam.
- Markdown is normalized on store (S6 caveat, reconfirmed: trailing
  whitespace changes) — nothing may compare bodies byte-exact.
- The summary-staleness rule (ADR 004) is untouched on the tool path,
  but a human editing a body in the UI does not flip `summary_stale`;
  whether resync should infer that is left open in WP9.
- The index gets lighter and hydration payloads smaller; nothing else
  about ADR 002's projection design changes.
