# ADR 052: Span-level review marks — approve, lock, and flag a selection

Date: 2026-07-28
Status: accepted

## Context

WP42's review state was per paragraph; the user wants to highlight an
arbitrary stretch of text on the prose page and approve / lock / flag
exactly that portion (chosen explicitly over per-paragraph state during
the WP44/45 planning, accepting new anchoring machinery). The classic
objection — spans drift under edits from two writers and need stored
offsets plus re-anchoring — dissolved once WP45 landed: the log already
carries state across edits per TOKEN by positional inheritance. Span
state is the same walk with a different payload; nothing offset-shaped
is ever stored.

## Decision

### Marks gain a token range; state lives on tokens

`SectionMark` grows optional `start`/`end` — a token range over the
block's normalized text at MARKING TIME (wire keys `s`/`e`; absent =
whole block, so pre-WP46 marks read unchanged and old readers ignore
the extra keys). The block-level fold is replaced by
`revisions.token_states(entries)`: per-token `status` and `intent`
carried through revisions by the SAME inheritance authorship uses
(`closest` ancestors without match consumption; token-equal ranges
carry state, new tokens default). Consequences that fall out rather
than being coded:

- **An AI edit voids `approved` exactly on the words it changed** —
  changed tokens are new tokens (default `raw_ai`), untouched approved
  words stay approved. This deliberately refines WP42's block-wise
  void; ADR 050's fold rules are superseded on this point.
- A human edit keeps state on every untouched word; the words the
  human typed read `human`/`flexible`.
- A mark applies at its fold position iff its hash is live; ranges
  clamp to the text the block has there (a range the text no longer
  covers folds to nothing). Compaction's mark hoisting is unchanged —
  a hoisted range stays valid because the hash names the same text.
- Restored-verbatim blocks keep their token state (identity rides the
  hash, as with authorship).

`section_states` survives as the derived BADGE view: a block is
`approved`/`human` only when every token agrees (mixed reads `raw_ai`
— the word-level display shows the split); intent is the strictest
token's, so one locked word makes the block read locked. Everything
block-shaped (edit_document listings, page badges, context-block
`[§hash · intent]`) sits on this derivation unchanged.

### Locked enforcement: verbatim-run presence

`missing_locked` now demands each contiguous LOCKED token run's text
appear verbatim in the new body's normalized text (`locked_runs` →
substring presence, still order-insensitive, still no diffing). This
both narrows and widens WP42's hash-presence rule, deliberately:

- The model may now edit the REST of a partially locked paragraph —
  only the locked words themselves must survive.
- Locked text may move anywhere, even into another paragraph (the old
  rule pinned the exact block; "the text survives verbatim" is truer
  to intent). Splitting a paragraph mid-run still violates (the run
  gains a block boundary) — strict, and the error teaches.

`LockedSectionsChanged` carries the missing runs' text (capped ~240
chars each); the context block spells out `locked verbatim: "…"` under
partially locked blocks so the model knows the exact words before it
writes (fully locked blocks keep just the badge).

### Surfaces

`record_mark` takes `start`/`end` (both or neither; validated against
the block's token count; change-only applies per slice). The bridge's
`node_view` replaces `authorship` with merged `spans` —
`[author, status, intent, text]` runs over normalized text — plus
`token_lens` (selection offset → token index mapping) and `glue` (the
raw heading marker, rendered before the spans and excluded from
offsets). `POST /api/prose/mark` accepts optional integer `start`/`end`
(same auth gates). On the page: authorship keeps its background wash;
state renders as underline decorations (solid = approved, double =
locked, wavy = needs change) so the two compose; selecting text opens
the same action bar with one token-ranged target per touched paragraph
(a multi-paragraph selection marks each intersected range), and a plain
tap still marks the whole block. After an apply the view re-renders in
place, scroll preserved.

## Consequences

The page can now express "these three sentences are canon" without
freezing the paragraph around them. The mark log grows one line per
gesture, as before. Sub-token precision (half a word) intentionally
does not exist — the token is the atom, matching authorship. WP42's
`test_human_edit_inherits_status_and_intent_via_similarity` pin was
rewritten to the token semantics (the block badge of an edited
approved block drops to `raw_ai` while its untouched words stay
approved) — that behavior change is this ADR's headline, not a
regression.
