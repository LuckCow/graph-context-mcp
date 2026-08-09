# ADR 051: Human-revision roll-up and derived word-level authorship

Date: 2026-07-28
Status: accepted

## Context

Dogfooding the ADR 050 prose page against a live space exposed two
problems on the road to the page becoming the primary prose editor:

1. The change tick records a human revision every ~5 s while the user
   types in Anytype — a ten-minute editing session becomes dozens of
   noise revisions, drowning the timeline and fragmenting blame lineage
   (each tiny hop re-runs the similarity match).
2. Block-level blame says *a* human touched a paragraph; the user wants
   the exact words each author contributed visible inside the block.

The larger editor vision (recorded here so the follow-up phases start
from decided ground): approval state will move to true SUB-PARAGRAPH
spans (the user chose this over per-paragraph state, accepting that it
reintroduces span-anchoring machinery ADR 049 avoided); in-page editing
with selection-to-approve is the next stage; an embedded chat panel
follows, one session per document (`prose:<node-id>` — the session
registry is namespace-agnostic, verified) driven as async turn jobs
with SSE progress, because real turns run 14 s median / 78 s p90
against the bridge's 15 s call timeout.

## Decision

### Roll-up: consecutive human revisions coalesce into one

`revisions.rollup_base(entries)` (pure) returns the log minus a
coalescible human tail: the tail must be a human `RevisionRecord`
(keyframe|delta) AND the LAST log entry. The historian's `_record`, on
a human recording, re-records against that trimmed base — the
replacement reuses the dropped tail's seq, and keyframe cadence is
seq-derived, so it survives; `known_hashes` re-derivation makes
tail-only block texts re-carry automatically. Solidification is
structural, not stateful: a model revision or ANY section mark becomes
the last entry and the predicate refuses — the next human edit opens a
NEW revision. A mark solidifies deliberately: it pins exactly the text
that was reviewed.

Rules that make it safe:

- The full-baseline no-op guard runs BEFORE roll-up: idle ticks with an
  unchanged body never rewrite the sidecar (they would otherwise churn
  a fresh `at` forever).
- **Revert-to-base:** if the new body equals the pre-tail state (the
  human undid the whole session), the pending revision is REMOVED —
  the log is rewritten without it, never an empty delta. `_record` now
  means "was the sidecar rewritten".
- **Refuse guard:** when the trimmed log still contains records but no
  usable keyframe (the post-compaction `truncated-marker + keyframe`
  shape, where the human tail IS the first kept keyframe), roll-up is
  refused — re-recording there would regress seq behind the marker and
  could strand deltas. Cost: one extra revision in an already
  pathological log.
- `at` is latest-wins: the record represents one editing session and
  every consumer (timeline, blame, fold) reads `at` as "as of".
- No new persistence: the predicate reads only the log, so restart +
  `rebuild()` keeps coalescing into the same pending tail.

### Word authorship: a derived per-token view, nothing stored

`revisions.word_authorship(records)` walks the log once, carrying a
per-TOKEN author (the existing `_WORD` tokens over NORMALIZED text) for
every hash ever seen. An added block resolves its ancestor through
`closest` — WITHOUT consuming matches: authorship is lineage, not a
one-to-one pairing, so a paragraph split in two lets BOTH halves
inherit the original's words (`revision_diff`'s display pairing keeps
its greedy discard deliberately). Token-equal ranges (SequenceMatcher
over token lists) copy the ancestor's authors positionally; everything
else takes the introducing revision's author. A hash seen before keeps
its authorship when restored verbatim — identity rides the hash. The
`MIN_BLAME_CHARS` floor applies (blame's rule, one place); truncated
history degrades to introduced-at-keyframe authorship.

Display: spans cover NORMALIZED text (the diff view set this
precedent). The bridge re-prepends a raw leading-heading marker onto
the first span so headings don't visibly flatten, and fence-containing
blocks degrade to raw rendering — never render code normalized. The
page renders human spans as a subtle `<mark>` highlight; `null`
authorship (unrecorded edits, short blocks, fences) renders exactly as
before. No caching: the compaction cap bounds the log and `node_view`
already does comparable similarity work in blame; the natural cache
point, if ever needed, is a lazy `_Baseline` field.

## Consequences

Roll-up shortens authorship chains (one hop per editing session), so
the two features compound. The revision timeline now reads as sessions;
`r2 human +1 −1` means one whole editing pass. Blame's `at` for a
coalesced revision is the session's last touch. WP42's fold semantics
are unchanged — marks still apply at file position, and a mark mid-
session splits the session into reviewed and post-review revisions.
