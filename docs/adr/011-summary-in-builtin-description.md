# ADR 011: Summaries live in Anytype's built-in description property

**Status:** Accepted (2026-07-02) — WP10b; retires the `gc_summary` key
(the checkbox `gc_summary_stale` is unchanged)

## Context

The summary is the node's most important one-liner: every `explore` hit
renders it, the staleness lifecycle (ADR 004) guards it, and the LLM is
told to keep it current. Yet it was stored in `gc_summary`, a custom
property the Anytype UI buries in the relations panel — invisible in Set
rows, object previews, and under page titles.

Anytype has a built-in `description` property that the UI features in
exactly those places. A live spike (2026-07-02, GC-E2E) confirmed the two
facts this decision needs:

- The built-in `description` is **writable through the API** — settable at
  object creation and via PATCH like any text property.
- It is **returned in list/search responses** — so unlike the body (A7,
  ADR 010), text stored there rides the hydrate sweep. Summaries must live
  in the `GraphIndex`; this is what makes the built-in property eligible
  where the body was not.

A scan of the real story space found no competition for the slot: zero
objects had the built-in description set, and only nine carried a
`gc_summary` (most of the world was human-authored in the UI and never
had a summary at all).

Naming note: after ADR 010, "description" in *tool-surface* language means
the long-form body. Anytype's built-in `description` property is a
different thing — a one-liner slot the UI features — and this ADR uses it
as the **summary channel**. The tool surface keeps the words `summary`
(one-liner) and `description` (long form); only the storage key changes.

## Decision

1. **`Node.summary` is stored in the built-in `description` property.**
   `mapping.PROP_SUMMARY` becomes the literal key `"description"`; the
   domain, tools, and presenters keep the `summary` name everywhere.
2. **`gc_summary` is retired** to a legacy constant used only by the
   migration script, mirroring the WP9 pattern: a read fallback in
   `to_node` (built-in first, `gc_summary` otherwise) bridges unmigrated
   spaces, and is deleted once the real space is migrated. Bootstrap no
   longer mints `gc_summary`; the built-in property exists in every space,
   so nothing needs minting in its place.
3. **`gc_summary_stale` stays as-is.** The staleness rule (ADR 004) and
   its checkbox are orthogonal to where the summary text lives.
4. **WP10a must exclude the built-in `description` from generic field
   reflection** — it is the summary channel, not an attribute (pinned by
   test when 10a lands).

## Consequences

- Summaries become visible everywhere a human looks — under the title, in
  Set rows, in previews and graph hovers — with zero UI configuration.
- Humans will now *edit* summaries in the UI (the point). An out-of-band
  summary edit reaches the index via resync like any property edit;
  last-write-wins versus the bot stands (WP1), and a human edit does not
  flip `summary_stale` — same as before, just more likely to occur.
- The SessionContext bookkeeping node's one-liner also moves to the
  built-in property (it goes through the same mapping) — harmlessly
  human-visible where it was invisible before.
- One fewer `gc_` key; the thin infra layer shrinks to `gc_summary_stale`,
  `gc_story_time`, `gc_fields`, and the two infra types.
- **New quirk A8, found by the live E2E the day this shipped:** the
  markdown *export* on single-object GET prepends the built-in
  description as its first line, while PATCH writes body blocks only — a
  raw GET → PATCH round-trip would duplicate the summary into the body.
  `mapping.body_of` strips the prefix (every server read goes through
  it), the mock composes the same export, and any future write-back path
  (e.g. WP10c's connections footer) must write `body_of` output, never
  the raw `markdown` field.
