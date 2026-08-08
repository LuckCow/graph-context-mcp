# ADR 056: Prose editor — highlight view modes and comments

Date: 2026-08-03
Status: accepted (extends ADR 050/052/054)

## Context

The prose page stacks four word-level highlight classes — locked (red),
needs_change (amber), approved (green), human-typed (lavender) — with
first-match-wins priority (ADR 053 amendment). Dogfooding found two
gaps:

1. **The layers are illegible together.** Priority also *masks*: a
   locked word's red hides its lavender authorship; a page full of
   mixed state reads as noise.
2. **There is no channel for the human to talk ABOUT the text.** Notes
   like "make this darker" went through chat, where they lose their
   anchor to the words they discuss and scroll out of the model's
   memory. Review state (WP42/46) says *what* a section is, never
   *what the human wants done to it*.

## Decision

### View modes: the legend becomes the control

A six-way mode — `all · authorship · status · intent · comments ·
none` — rendered as chips where the legend was (the swatches double as
samples; the active chip is lit). Selection is persisted in
`localStorage` (`gc_prose_view_mode`) and switching re-dispatches
decorations from the retained doc payload: no refetch, never through
the router (the CM view, scroll, and selection survive).

Single-concern modes **unmask**, not filter: spans carry the full
`(author, status, intent)` triple, so `authorship` shows lavender on
words the `all` priority paints red, `status` shows only approved
green, `intent` shows locked + needs_change. `spanClass` is the one
mode-aware seam. Blame line ticks stay on in every mode (gutter-edge,
not text background). Comment underlines render in `all` and
`comments`. Frontend-only — no wire change.

### Comments: notes on selections, stored in the sidecar log

A comment is human-authored (the prose page's action bar grows an
`add comment` composer beside status/intent), anchored like a ranged
mark — block hash + token range via `char_range_to_tokens`, the only
char→token door — and stored as two new line kinds in the SAME
revision sidecar log (ADR 049's fence):

```
{"kind":"comment","id":"c3f81a2b","hash":"<block>","text":"…","at":"…","by":"human:prose-page","s":4,"e":9}
{"kind":"comment_state","id":"c3f81a2b","value":"addressed","at":"…","by":"model"}
```

- **Identity**: `comment_id(at, by, hash, text)` — clock-free content
  hash; an identical same-second duplicate folds to the same id and
  the historian drops it change-only. Lifecycle `open → addressed →
  resolved` rides append-only `comment_state` lines (last-wins,
  `resolved` terminal); `open` is implicit and never serialized.
- **Anchors ride edits.** The `comment_states` fold tracks each live
  comment's anchor with the SAME lineage machinery as review state
  (`closest` similarity + positional `_inherit`, no match
  consumption): an edit moves the anchor to the successor block and
  keeps the surviving tokens; deleting the commented words falls back
  to the successor's whole block; deleting the block **detaches** the
  comment (`hash=""` — still listed until resolved, and a verbatim
  restore re-attaches it). Nothing offset-shaped is stored.
- **Compaction rewrites, not hoists.** A dropped-era live comment's
  anchor may have migrated during the dropped era, so mark-style
  `hash in live` hoisting would kill it. `compact` folds comments
  over the full log first and re-emits each dropped-era live comment
  REWRITTEN to its anchor as of the first kept keyframe (last-known
  anchor kept when detached), plus one `addressed` line when set.
  Resolved comments drop with their era.
- **The lifecycle is split by authority.** The MODEL may only mark a
  comment `addressed` — `edit_document action="address_comment"`
  (comment ids surface in the sections listing and the context
  block); addressing is sidecar bookkeeping, so it never journals,
  cards, or touches the body. Only the HUMAN resolves, on the page
  (`POST /api/prose/comments`, same gate order and base-token
  concurrency as save/marks; one route-lock hold, one sidecar
  rewrite). Addressed-but-unresolved stays visible to both sides as
  "awaiting human review".
- **The model sees comments where it sees the text.** The context
  block renders each live comment under its anchor block —
  `comment #id (open): "…" — on: "<the anchored words>"` — with
  detached ones trailing the body; `edit_document`'s sections listing
  appends a comments section plus one teaching line. Comment lines
  ride the body, so budget rung 2 drops them with it.
- **The page renders comments twice**: dotted underlines stacked on
  top of state backgrounds (dashed + muted once addressed), and a
  panel below the editor — state badge, text, author, resolve button;
  row-click selects and scrolls to the anchor, clicking an underline
  scrolls to the row (plain `domEventHandlers`, no tooltip machinery,
  no bundle rebuild). The composer is guarded: while it is open,
  `updateBar` never rebuilds the bar rows, so an SSE refetch cannot
  destroy a half-typed note.

## Consequences

- A comment (or transition) line ends the log's revision tail, so it
  **solidifies the WP44 pending human roll-up** — deliberate: a
  comment pins exactly the text it discusses; an autosave session
  bisects into two revisions around it.
- Old readers skip the new line kinds leniently, but a pre-056 WRITER
  re-renders the log from its parsed entries and would silently drop
  comment lines. Accepted: single-version deployment (same stance as
  WP42's mark lines).
- The wire's `comments` entries carry absolute code-point anchors
  derived at payload time (`token_range_to_chars`, `block_offsets`);
  detached comments serve `anchor: null` and are panel-only.
- The bare MCP server has no historian: `address_comment` degrades to
  a teaching error there, and nothing can create comments.
- Model addressing bumps the SSE version ledger like every sidecar
  write — an open clean page restyles the underline live.
- `_inherit` became generic (`TypeVar`) to carry the comment fold's
  boolean flag vectors; the review folds now guard on
  `isinstance(entry, RevisionRecord)` so any future line kind is
  inert by construction.

## Amendment (2026-08-03): comment editing + the desktop layout pass

The page grew a desktop layout, and comments became editable:

- **A third line kind, `comment_edit`** (`{id, text, at, by}`), folds
  last-wins onto the live comment's TEXT — id, anchor, and creation
  stamps stay, so the thread keeps its identity (the content-hashed
  `comment_id` seals the ORIGINAL text; edits never re-key it).
  Editing an `addressed` comment REOPENS it, stamped with the edit —
  the model's action answered the old wording, not this one. Unknown
  ids and resolved comments fold to nothing; an edit line solidifies
  the WP44 roll-up like every non-record line. Compaction bakes
  dropped-era edits into the hoisted comment line's rewritten text.
  `NodeHistorian.edit_comment` follows the mark discipline (validate
  against the current baseline, change-only no-op on unchanged text,
  unknown-id error lists the live ids); the wire is a third mutually
  exclusive `POST /api/prose/comments` operation, `edit: {id, text}`,
  human-authority like resolve — the model still only addresses.
- **Desktop layouts** (the mobile page is unchanged below 900px): at
  ≥900px the action bar docks under the header as a PERSISTENT
  toolbar — disabled controls when nothing is selected, a fixed-width
  ellipsized selection readout, and color-only `.current` styling, so
  the bar never changes shape as the selection does. At ≥1140px the
  comments panel rides a sticky sidebar beside the editor column;
  below that it opens as a modal overlay (one panel node reparents
  between sidebar and modal — listeners survive, nothing renders
  twice). Inline comment edits keep their draft in JS state keyed by
  comment id, so SSE refetch re-renders re-seed the textarea — the
  composer guard's discipline, extended to the panel.

## Amendment (2026-08-08): independent layer toggles, overlap made visible

The six exclusive view modes above are **retired**. Dogfooding found
both ends unusable while editing: `all` still masked (its priority
ladder answers "the loudest thing about this word", never "is this
approved *and* locked *and* mine?"), and the single-concern modes
answered one axis at the price of hiding the other two, so reading the
full state of a word meant cycling chips. Unmasking-by-mode traded one
kind of blindness for another; nothing showed the layers *together*.

- **Every layer toggles independently.** Seven keys —
  `locked · needs_change · minor_revisions · approved · human ·
  comments · blame` — each on/off, persisted in `localStorage` as a
  comma-joined enabled list under `gc_prose_layers` (absent key = all
  on, `""` = all off; WP50's `gc_prose_view_mode` is not migrated). The
  legend stays the control and grows `all` / `none` shortcut buttons;
  an off chip keeps its swatch as a dashed outline in the layer's own
  colour, so the legend still reads as a colour key. Blame line ticks
  became a toggle too — the earlier "on in every mode" carve-out was a
  consequence of mode exclusivity, not a decision.
- **A word shows every layer it carries.** `spanClass` (one class per
  span) becomes `spanLayers` — the ordered list of *enabled* layers a
  span matches — plus `spanMark`, which renders it: the first layer
  paints the background fill (the ADR 053 priority ladder survives
  intact, now applied to the enabled subset), and each remaining layer
  stacks a 3px solid bar under the word through a `--hl-bars` custom
  property that one `.cm-hl` rule composes. Intent is single-valued, so
  a span carries at most three text layers → one fill and up to two
  bars, and a single-layer word looks exactly as it did before.
- **Bars are `box-shadow`, not `border-bottom`.** An outer shadow is
  clipped to outside the border box (a clean stripe, never a wash over
  the word) and costs no layout, so marking text cannot jostle line
  heights. The class vocabulary is now derived from the layer key on
  both sides — `.cm-fill-<key>` in the editor, `.legend mark.sw-<key>`
  in the legend — replacing the two hand-synced class sets, and a test
  pins the correspondence.

Unchanged: spans still carry the full `(author, status, intent)` triple
and comment underlines still stack on top of state backgrounds; toggling
re-dispatches decorations from the retained payload — no refetch, never
through the router. Frontend-only, no wire change.

## Amendment (2026-08-08): the chrome above the manuscript

Both controls above the editor grew without bound as a document aged;
the manuscript is what the page is for, so both were condensed.

- **The legend moved into the frozen chrome**, below the header (and,
  on desktop, below the docked action bar), where it stays reachable
  however far the reader has scrolled — previously it scrolled away
  with the column, so changing a layer meant scrolling back up. The bar
  itself is **collapsible** from a `highlights` header button
  (`gc_prose_legend_open`, default open — the colours have to be
  discoverable before they can be dismissed), because seven chips wrap
  to three lines on a phone. The legend node is now static page
  furniture rather than per-render markup: it only shows with a
  document open (`body.has-doc`) and empties on `closeDoc`. The
  affordance hint stays desktop-only, and the read-only variant is
  gone — the header note already carries it.
- **Revision history is one dropdown**, newest first, replacing the
  chip-per-revision strip: the strip grew unbounded and pushed the
  editor off the first screen on any document with real history.
  Picking a revision routes to its diff exactly as the chips did; the
  closed control reads `N revisions · pick one to diff` beside a
  `last <author> · <when>` note, and each option carries the detail and
  `+added −removed` counts the chip titles used to hide.

Frontend-only, no wire change: the `revisions` payload is untouched.
