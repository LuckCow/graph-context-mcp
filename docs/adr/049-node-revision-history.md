# ADR 049: Paragraph-hash revision history for tracked node types

Date: 2026-07-27
Status: accepted

## Context

With documents living in nodes (ADR 048), the user wants to see how a
chapter's text changes and who changed it — git-blame-style per-section
authorship (AI vs human), and later (WP42/43) per-section approval
status, revision intent with harness-verified LOCKED sections, and a
review UI. Nothing in the system remembers what a body used to say:
bodies never enter the index, resync can't see body edits (only
`modified_at` moves), and ADR 010 forbids byte-exact body comparison —
the store normalizes markdown (plus quirks A8/A9/A13 and the ADR 013
footer rewrites). Any span/offset scheme would drift under edits
arriving from two independent writers (the bot and the human in the
Anytype UI).

## Decision

### Identity: normalized block hashes, never offsets

A body splits into markdown BLOCKS (blank-line separated, fences kept
whole); a block's identity is a 16-hex sha256 of its NORMALIZED text
(leading-`#` strip absorbs A9, fence-info strip absorbs A13, whitespace
collapses). An unchanged paragraph keeps its identity across moves and
across edits elsewhere — zero re-anchoring machinery, and Phase 3's
locked check becomes set membership ("every locked hash still present"),
no diffing on the hot path. All rules live in ONE pure module,
`domain/revisions.py`; nothing else may compare bodies.

### Storage: one sidecar node per tracked node, keyframe+delta log

Each tracked node gets ONE hidden `gc_node_history` sidecar (infra role
`NodeHistory`; discriminator `gc_history_of`, a TEXT property on the
`gc_session_key` pattern — no edge reflection, no footer interplay).
The sidecar BODY is the append-only log: a header sentence, then one
JSON record per line inside a single unmarked fence (no info string —
A13 would eat it; no heading-shaped first line — A9 would flatten it).
Records are keyframes (full hash sequence, every `KEYFRAME_INTERVAL` =
20) and deltas (SequenceMatcher opcodes over hash sequences; deletes
implicit), with `new_blocks` carrying text only for first-seen hashes.
Parsing is LENIENT: an unparseable line is skipped and counted, never
fatal — a human poking the sidecar degrades history, never bricks it.
The live server round-trips fence contents intact (contract test
`test_fenced_jsonl_body_round_trips_intact`; live-confirmed 2026-07-27,
`docs/spikes/node-history-body.md`).

Rejected: **per-revision nodes** ("lots of edits" would bloat every
index rebuild/resync forever and cost N startup fetches per chapter vs
one); **local file storage** (the user wants history to follow the
space); **tool-bound tracking** (a `record-on-tool-use` design punches
holes for every write that bypasses the tool — the historian must be
write-path-agnostic).

### Tracking: a Space Context list — data config, not mode config

The Space Context gains **`gc_tracked_types`** (text; comma/newline
separated type display names, `Tracked types` in the UI). A node is
tracked iff its type is listed (infra types never track, whatever is
typed). The historian reads the list off the INDEX by role — the
rule-engine pattern — so a human edit applies within one change tick,
no restart. Mode-scoped tracking was rejected: ProseWeaver also updates
character nodes, and a second mode editing a tracked chapter would have
misattributed its diff to "human" on the next tick.

### Recording: compare-to-baseline at two structural points

`application/node_historian.py` (`NodeHistorian`, one per space runtime)
keeps an in-memory baseline per tracked node and appends a revision only
when the CURRENT normalized hash sequence differs. Baselines come from
**`fetch_body` output, never the text we sent** — store normalization
drift can therefore never mint phantom revisions. Sidecars are created
lazily on the first body-bearing observation. Sidecar writes go straight
through the repository (dedicated infra writer, like the recorders) and
are never journalled — bookkeeping must not card.

* **Bot writes** — `_finish_turn`, post-drain: one attributed revision
  (`model · <mode> · <user>`) per touched tracked node per turn (the
  turn is the granularity). Mode-independent. Failures log and never
  fail the turn.
* **Everything else** — a third `history` listener on the ADR 044
  change tick: a body edit bumps `modified_at`, landing the node in the
  tick's resync `changed` set; `Orchestrator.history_tick` sweeps
  changed tracked ids as author `human`. The bot's own writes already
  advanced the baseline, so the tick compares equal — idempotent by
  construction, no self-write bookkeeping.
* **Restart** — `rebuild()` (bootstrap, after hydrate) reloads
  baselines from the sidecars, then one catch-up compare per tracked
  node records offline edits (ADR 019's offline promise). Clean
  replays record nothing.

### Derived views, compaction

Blame is computed from the log at read time — never stored: each
current hash is blamed to the revision that introduced it, with lineage
via `difflib` similarity (threshold 0.6) against the same revision's
removed blocks. Sub-20-char blocks (scene separators) stay out of
blame. Compaction keeps the newest keyframe chain under
`LOG_SOFT_CAP` = 400 K chars, prepends a truncation marker, and
re-carries still-alive block texts onto the first kept keyframe so
current-state blame always survives (user decision: dropping the oldest
layers beats archive-node sprawl).

## Accepted failure modes

* A human edit landing between the bot's read and PATCH is lost in the
  STORE (wholesale `markdown` replace) — unfixable here; both
  surrounding states still record.
* A crash between the document PATCH and the sidecar append attributes
  that one diff to "human" on the next tick. Rare, self-healing.
* Human revisions attribute to the generic `human` — the API exposes no
  last-modified-by identity (open spike).
* Heavy churn eventually sheds the oldest layers (compaction).
* An UNPINNED future store-normalization quirk would surface as phantom
  human revisions — visible in the log, not corrupting.
* Tracking a high-churn type costs one `fetch_body` per changed tracked
  node per tick — fine at chapter scale; don't track types a rule or
  automation rewrites every few seconds.

## Consequences

Phase 3 (WP42) keys per-block status (`raw_ai|approved|human`) and
revision intent (`locked|flexible|needs_change`) off the same hashes in
the same log, enforces locked sections as one rule in `NodeWriter`, and
adds a block-anchored `edit_document` tool sharing this module's anchor
vocabulary. Phase 4 (WP43) renders blame/status on an inspect-server
page. The bare MCP server has no historian (orchestrator feature, v1).
