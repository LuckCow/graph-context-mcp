# ADR 050: Section review state, locked enforcement, and the prose review page

Date: 2026-07-28
Status: accepted

## Context

ADR 049 shipped the revision backbone: paragraph-hash identity, the
keyframe+delta sidecar log, derived blame. Its phase roadmap left three
things open — per-section review state (WP42), a locked-section
guarantee the model cannot talk its way around, and the human review
surface (WP43). The user reads on a phone over Tailscale, so the page
must be mobile-first; ADR 025's house style (stdlib server, one
self-contained vanilla-JS page, no build step — the egress firewall
makes dependencies a container-image change) still governs, and the
inspection server's read-only-by-construction posture was an explicit
load-bearing safety property that a write surface must consciously
amend. An Anytype-UI fork was considered for the surface and rejected:
the desktop and two mobile clients are three separate fast-moving
codebases and no plugin API has shipped.

## Decision

### Marks live in the revision log; a fold derives current state

Two new line kinds join the SAME sidecar JSONL (ADR 049's lenient
parse absorbs them; pre-WP42 readers skip them as unparseable lines):
`{"kind":"status"|"intent","hash","value","at","by"}` — no `seq`, file
order IS fold order. Vocabulary: status `raw_ai | approved | human`,
intent `locked | flexible | needs_change` (`domain/revisions.py`, the
only home). `section_states(entries)` folds the interleaved log into
current per-block state, keyed to the final hash sequence:

- A mark applies iff its hash is live at that point; stale marks fold
  to nothing (harmless by construction).
- Lineage uses THE existing blame rule (`closest`, difflib ≥ 0.6 —
  promoted to public; one similarity rule in the system).
- Across a HUMAN edit the successor block inherits its ancestor's
  status AND intent (the roadmap's "statuses follow"); a no-ancestor
  human block starts `human`. A human edit of a `raw_ai` block stays
  `raw_ai` — editing is not approving; approving is one tap.
- Across a MODEL edit the successor drops to `raw_ai` (any AI edit
  voids `approved`) while intent follows — `needs_change` surviving an
  AI rewrite is deliberate (the drop to `raw_ai` already flags
  re-review), and `locked` cannot legally reach this path at all.
- Compaction hoists dropped-era marks whose hash survives in the first
  kept keyframe to just after it (fold-equivalent); marks on dead
  hashes drop with their era. Revision-DERIVED state (an inherited
  `approved` with no surviving mark line) does not survive compaction —
  accepted: the page shows `raw_ai`, the human re-taps.

### Locked enforcement: one rule, in NodeWriter, via an injected guard

`NodeWriter.update_node` consults an optional `SectionGuard` protocol
(structural — the writer never imports the historian) whenever a body
is written: `historian.check_body_update` runs the fold and an
ORDER-INSENSITIVE presence check (`missing_locked`) — moving a locked
block is fine, changing or deleting it raises `LockedSectionsChanged`,
an errors-are-prompts message carrying the missing sections' text and
the escape hatches (reproduce verbatim / `edit_document` other sections
/ ask the user to unlock). Wiring is the ADR 045 pattern's sibling:
`Services.historian` is LATE-BOUND by the orchestrator bootstrap after
`historian.rebuild()`, and `derive_services` passes it into every
session writer — the bare MCP server never sets it (no historian, no
enforcement, ADR 049's v1 scope). The dedicated infra writers
(scheduler, rule engine, recorders, the historian itself) bypass
NodeWriter and therefore the guard — locked is a contract with the
MODEL, not a storage ACL; the human in Anytype can always edit.

### edit_document: hash-anchored single-section edits

A new mutation tool (bound only in `mutating` modes, unlike
schedule/automation): `sections | replace | insert_after | delete`,
anchored on the block-hash vocabulary (unique git-style prefixes
accepted; `top` prepends). `application/document_editor.py` composes
`fetch_body → revisions.edit_body → writer.update_node`, so the guard,
journal, staleness rule, and infra validation apply with zero new
enforcement points; untouched blocks are spliced verbatim, keeping
their hashes (and marks) by construction. Anchor misses raise
`SectionAnchorNotFound` listing the real anchors. The context block
renders tracked FULL-entry bodies per block as `[§hash · intent]`
(default intent renders bare) — the anchors and constraints ride into
every turn; ADR 048's guidance now points revisions at the tool.

### The prose page: bridge, first non-GET route, mobile-first

`orchestrator/prose_bridge.py` is the ONLY seam between the inspection
server's daemon thread and the bot loops: a threading.Lock-guarded
registry of per-space handles (created EMPTY before bots bootstrap;
spaces register as runtimes come up), where every read AND write is a
coroutine scheduled via `asyncio.run_coroutine_threadsafe` onto the
owning loop (baselines are loop-thread state; even reads would race) —
`set_mark` additionally takes the space's route lock so marks never
interleave with a turn. A 15 s result timeout maps to HTTP 504.

Routes: `/prose` (the page), `/api/prose/spaces|node|diff` (GET, JSON;
word-level intra-block diffs computed SERVER-SIDE with the same difflib
the domain already uses — no client diff library), and the server's
first `do_POST`, `/api/prose/mark`. Writes are doubly gated: a
same-origin check (`Sec-Fetch-Site` when present, else `Origin` vs
`Host`) refuses drive-by browser pages, and a shared bearer token
(`GC_PROSE_TOKEN`, off-value conventions; unset = read-only page, 403)
authenticates the human — compared with `hmac.compare_digest`, 401
distinct from 403 so the page knows when to prompt (token cached in
localStorage). GETs stay tokenless; 409 = stale hash (reload), 404 =
unknown space/node. `prose.html` follows the inspect.html house style
(hash routing, `createElement`/`textContent` only) plus a viewport
meta, thumb-reach fixed action bar with ≥44 px targets, blame-colored
left borders, and `prefers-color-scheme` dark — the first
mobile-responsive page on this server. Network reach (Tailscale) is
deliberately an ops concern; nothing in the code assumes it.

## Accepted failure modes

- **Guard freshness window:** the guard folds the BASELINE log; a human
  edit between change ticks isn't recorded yet. A locked block the
  human just deleted can raise a false `LockedSectionsChanged` (model
  retries after the next tick), and `record_mark` validates against the
  recorded body, rejecting marks on just-edited sections with a 409
  ("reload"). Both self-heal within one tick.
- **Mark growth:** every tap appends a line; the existing compaction
  cap bounds the log, and the hoist rule keeps live marks.
- **Normalized historical text:** the log stores normalized block text,
  so diffs and guard excerpts show headings flattened; the page says so
  on the diff view. Current-body views always use raw text.

## Amendment (2026-08-08): the `minor_revisions` intent

The intent vocabulary gains a fourth value between "touch nothing" and
"rewrite this": `minor_revisions` — KEEP the content and stay as close
to the original intent as possible, improving only sentence structure,
organization, and word choice. It is a plain vocabulary addition: a
`domain/revisions.py` constant in `INTENT_VALUES`, so the historian's
mark validation, the sidecar log, the fold, the page's action bar, and
the section badges carry it with no new mechanism, and it is *not*
enforced (unlike `locked` it binds no writer — it is an instruction to
the model, spelled out once in the `edit_document` tool doc alongside
the other three).

Mixed-token blocks badge as the LOUDEST intent, an order now named
once (`revisions._INTENT_BADGE_ORDER`, mirrored by the page's optimistic
patch): `locked > needs_change > minor_revisions > flexible`. A block
where the human wants some words reworked and others merely polished
reads `needs_change` — the stronger call to action surfaces; the
word-level view shows the split. The page paints it cyan (`--info-bg`),
kept clear of the red/amber/green/lavender state hues, at the matching
priority in both `all` and `intent` view modes (ADR 056).
