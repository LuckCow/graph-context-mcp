# ADR 053: In-page prose editing, plain HTTP, no editor library

Date: 2026-07-28
Status: accepted

## Context

The prose page is becoming the primary editing surface (the stated
goal: replace Anytype for prose). Two questions the user raised
directly: do we need websockets, and do we need a text-editing library
(ProseMirror/CodeMirror class)? Also: the WP46 state underlines should
be subtle highlight colors instead.

## Decision

### No editor library; the paragraph is the editing unit

Editing libraries earn their keep for rich-text WYSIWYG and
collaborative cursors. Our unit is the markdown BLOCK — the same atom
the revision log, marks, and `edit_body` splice already speak. The page
swaps a plain `<textarea>` into the block card (edit / insert-below /
delete on the tapped card's action bar, plus an add-paragraph button at
the document end); save POSTs and the view re-renders with fresh
anchors and blame. Zero dependencies, no build step — ADR 025's rule
stands because the egress firewall makes any dependency a
container-image change.

### No websockets; POST + (later) SSE

Websockets buy bidirectional realtime — multi-cursor collaboration.
This editor is turn-based with one human: writes are plain POSTs, and
any future server→page push (auto-refresh when the bot revises a
chapter) fits the SSE pattern `/events` already uses. Decision:
`POST /api/prose/edit` (same origin + `GC_PROSE_TOKEN` gates as marks;
body cap 256 KB) with actions `replace | insert_after | delete`
anchored on block hashes; a stale anchor is 409 (reload and retry),
mirroring stale marks. Live auto-refresh is deliberately deferred.

### The edit path is a HUMAN edit, end to end

The bridge's `edit_block` runs on the bot loop under the route lock:
`fetch_body → revisions.edit_body → repository.update_node → 
historian.record_external_revision(detail="human:prose-page")` — the
revision records IMMEDIATELY (no change-tick wait), so the refreshed
view shows fresh blame, and it coalesces into any pending human
revision (WP44) with the page's attribution. Two deliberate choices:

- **The locked guard does not apply.** Page edits write through the
  repository like Anytype-UI edits, not through NodeWriter: locked
  binds the MODEL; the human who locks text is the authority to change
  it.
- **Self-write coherence is the historian's no-op guard**: the next
  change tick sees the node modified, re-fetches, and compares equal —
  nothing double-records.

### State indicators are subtle highlights (amends ADR 052)

Underline decorations are replaced by one background tint per word,
chosen by priority: locked (red) > needs_change (amber) > approved
(green) > human-typed (lavender — moved off the green it shared with
approved). One highlight per word keeps mixed text readable; the
authorship wash yields to an explicit review state on the same word.

## Consequences

Editing, marking, and reviewing now live on one page; Anytype remains
the storage and fallback surface. The remaining stage-4 item is the
embedded chat panel (per-document `prose:<node-id>` sessions, async
turn jobs + SSE). If simultaneous editing from two devices ever
matters, 409-on-stale-anchor is the concurrency story — last write
wins per paragraph, never silently.
