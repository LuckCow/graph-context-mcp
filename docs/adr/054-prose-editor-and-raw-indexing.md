# ADR 054: The prose editor — CodeMirror, raw indexing, a document wire

Date: 2026-07-28
Status: accepted (supersedes ADR 053's no-library/block-editing
decisions; amends ADR 050's page architecture and ADR 052's mark
indexing; amends ADR 025's no-dependency rule for one vendored asset)

## Context

The WP42–47 prose page did not survive contact with real use. The
block-card model — tap a paragraph, edit raw markdown in a swapped-in
`<textarea>`, save, watch the whole page re-render — made ordinary
writing feel like form-filling, and the page blanked on every action
(`renderNode` emptied the DOM *before* its fetch; every mark and save
triggered a full re-fetch, one sequential POST per selected paragraph).
Underneath sat a real domain defect the page only papered over: span
highlights and token-ranged marks indexed the NORMALIZED block text
(ADR 049's identity form) while edits wrote RAW markdown — reconciled
with a `glue` heading-marker hack, fenced blocks degraded to inert
text, and everything under `MIN_BLAME_CHARS = 20` (short dialogue!)
unmarkable by fiat. The user asked for a total rework: a real text
editor with direct inline editing, span labels for intent
(locked/flexible/needs_change) and review status, and no blank-page
refreshes — explicitly open to an editor library.

## Decision

### One editor library, vendored prebuilt: CodeMirror 6

The page is now one continuous CodeMirror 6 editor over the whole raw
body — type anywhere; markdown is shown styled-raw (headings large,
markup dimmed), never serialized through a rewriter. CM6 over
ProseMirror-class WYSIWYG deliberately: a markdown serializer rewrites
source text (escaping, emphasis chars) on every edit, which would
reintroduce exactly the two-text drift this ADR eliminates; in CM6 the
editor buffer IS the node body, byte for byte.

ADR 025's "no dependencies" rule is amended, not repealed: no build
step lives in the repo or CI. The bundle
(`orchestrator/static/codemirror.bundle.js`, ~500 KB minified ESM, MIT)
is produced by a documented one-off esbuild run
(`scripts/vendor/codemirror/`) and checked in; the inspection server
grows its first static route (`/static/<name>`, `safe_child`
containment, 404 on missing — `_serve_file`'s silent empty-200 died
with it) and `create_server` verifies the bundle ships like the three
pages.

### Identity stays normalized; every text index goes raw

`normalize_block` is now ONLY the hash input (ADR 010 holds: identity
must absorb store rewrites A9/A13). Everything else — stored
`new_blocks` texts, word tokens, mark ranges, authorship, locked runs —
indexes the raw block text as fetched. Sound because baselines already
come only from `fetch_body` output and the store is idempotent on its
own output. Consequences:

* **Drift self-heal = v1 migration.** `next_record` re-emits a hash
  whose stored text differs from the live raw text (`texts_of` is
  last-write-wins), and the historian's no-op guard lets a hash-equal
  but text-stale body record an all-equal delta. The first
  post-upgrade `rebuild()` catch-up therefore migrates every
  pre-ADR-054 (normalized-text) sidecar automatically — no wipe, no
  version gate. New records carry `"v": 2` for forensics only.
  Accepted skew: pre-upgrade *ranged* marks can land a token off on
  heading blocks; whole-block marks are exact.
* **`MIN_BLAME_CHARS` is dead.** Its real job was keeping duplicated
  scene separators (which share hashes) from carrying state; the rule
  is now `has_words` — word-free blocks stay unmarkable/unblameable,
  short prose ("No.") is fully markable. Fenced blocks no longer
  degrade at all. Duplicate identical paragraphs still alias one hash
  and share state — accepted, as before.
* **Locked runs are raw substrings**; `missing_locked` folds
  whitespace runs to single spaces on both sides before the presence
  check (store reflow is not a violation; still no byte-exact
  comparison and no diffing).
* New pure helpers: `block_offsets(body)` (absolute char offsets of
  identity-bearing blocks — the wire's segment map) and
  `char_range_to_tokens(text, s, e)` (the ONLY place the wire's char
  offsets become token indices; nothing offset-shaped is stored,
  ADR 052's marks-are-gestures rule intact).

### A document-level wire

* `GET /api/prose/doc` — full raw body + `base` (a digest of the hash
  sequence: the concurrency token; whitespace-only store reflow does
  not invalidate an open page) + `segments` (hash, offsets, badge,
  blame) + `spans` (absolute-offset merged author/status/intent runs;
  the client never tokenizes) + the revision timeline. Offsets count
  code points; the page converts to UTF-16 at the edge.
* `POST /api/prose/save` — whole-document save under the route lock:
  base mismatch → 409; else `repository.update_node` →
  `record_external_revision(detail="human:prose-page")`. Page saves
  remain HUMAN edits end to end and bypass the NodeWriter locked guard
  (ADR 053's authority argument stands). Returns the fresh doc payload
  so the client patches in place. Autosave (~2.5 s debounce) rides
  WP44's roll-up: an editing session coalesces into one pending
  revision, exactly as designed.
* `POST /api/prose/marks` — BATCH marks: one POST per selection
  gesture, one lock hold, one sidecar rewrite
  (`NodeHistorian.record_marks`; all-or-nothing validation,
  change-only requests drop out). Char ranges are relative to the
  block's raw text.
* `GET /api/prose/events?space` — SSE version bumps. The historian
  gained an `on_record` hook; the bridge registration wires it to a
  thread-safe per-(space, node) counter the SSE thread polls (same
  loop shape as `/events`). An open page reflects bot edits within a
  poll tick — ADR 053's "deferred SSE job", now shipped.
* Retired: `/api/prose/node`, `/api/prose/mark`, `/api/prose/edit`,
  the bridge's `node_view`/`set_mark`/`edit_block`, and the
  `glue`/`token_lens` machinery. Gate order (same-origin →
  writes-enabled → bearer → 256 KB cap → JSON) is unchanged and
  re-pinned on the new POSTs.

### The page

Decorations are a `StateField<DecorationSet>` built from the wire's
spans (priority locked > needs_change > approved > human-lavender,
per ADR 053's amendment) that MAPS through local edits — highlights
stay glued to words while typing, before any server round-trip. Blame
renders as an inset line tick per paragraph plus a caret-following
info strip. Selecting text opens the action bar (status/intent rows +
a whole-¶ toggle); apply is one batch POST. Nothing ever blanks: POST
responses patch decorations in place, SSE bumps on a clean buffer
reconcile via a minimal-diff transaction (scroll and selection
preserved), home/diff views render detached and swap. A 409'd save
opens a conflict banner — keep mine (overwrite with fresh base) or
take theirs; no three-way merge. Dirty-state indicator, Ctrl-S,
`beforeunload` guard; no token = read-only editor.

## Consequences

* The model's `edit_document` tool, the locked guard seam, historian
  record points, roll-up, compaction, and the context block's
  `[§hash · intent]` rendering are all untouched (locked-verbatim
  lines now show raw text — heading markers included).
* `word_diff` timelines over pre-upgrade revisions show normalized
  historical text; post-upgrade revisions show raw. Display-only.
* The jsdom smoke harness (scratchpad) exercised the full loop:
  mount, decorations, selection→batch marks in place, autosave with
  roll-up, SSE reconciliation. The vendored bundle re-verifies at
  `create_server` time.

## Rejected

* **ProseMirror/WYSIWYG** — serializer rewrites = new drift (above).
* **CDN script tag** — breaks offline and the egress posture; the page
  must work wherever the server does.
* **Document-level token stream identity** — would make paragraph
  moves lose authorship/state (delete+insert) and inflate the delta
  log; block hashes stay the log alphabet, offsets stay derived.
* **Three-way merge on conflict** — one human, turn-based writes; the
  banner's overwrite/discard covers the real cases.
