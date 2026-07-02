# ADR 013: A server-rendered connections footer in the body

**Status:** Accepted (2026-07-02) — WP10c

## Context

A node's relations are visible in Anytype only through the relations
panel; reading a page tells you nothing about what it links to. WP10's
theme is making each side's knowledge first-class on the other's surface —
and the graph is the server's half. The building blocks were all
established by earlier spikes: `anytype://object?objectId=…&spaceId=…`
markdown links are clickable, PATCH-stable link marks that never register
in `links`/`backlinks` (user-verified); one PATCH carries `markdown` +
`properties` together (A7); the markdown export prepends the summary, so
write-backs must be built from `body_of` output (A8); and every link write
already GETs the source object inside the single-writer critical section
(ADR 009), so the current body text is in hand at exactly the moment a
footer needs regenerating.

## Decision

1. **Story nodes get a generated footer section** at the bottom of the
   body: a `---` rule, a `## Connections (auto)` heading, and one line per
   **outgoing** relation — `label → [Target Name](anytype://…)`. Nodes
   with no outgoing relations get no footer. Infra-role nodes (Prose,
   SessionContext, intent) never get one: their bodies are write-once by
   policy.
2. **Outgoing only.** Incoming edges belong to the other object's footer;
   rendering them here would put deep links on the wrong side and, if
   Anytype ever mirrors body links again, mint wrong-direction edges.
   Footer targets are exactly the object's own semantic relation targets,
   so even a resurrected `links` mirror would be deduped by `to_edges`.
3. **The LLM never sees it.** `body_of` — the single body-read seam —
   strips the footer along with the A8 summary prefix, tolerantly (the
   store normalizes whitespace around the `---` rule). The model reads
   clean description text and gets edges from the graph, as always.
4. **Maintenance rides existing writes; no new API calls.** `add_link` /
   `remove_link` / composite-create incoming-link PATCHes already GET the
   source object; the same PATCH now carries the regenerated footer
   (clean body via `body_of` + fresh footer from the prospective edge
   set). `update_node(description=…)` renders the footer around the new
   text from the index's edges. A write that would not change the
   footer's rendered content does not rewrite the body (minimizes the
   pill-degradation surface, WP10c caveat).
5. **The server owns only the footer.** Text above the delimiter is never
   merged or edited; when no delimiter matches, the footer is appended.
   Stale footers (a human edited links or renamed a target out-of-band)
   are accepted until the server's next write to that node — the
   relations panel is always truth.

## Consequences

- A human reads a page and can click through its world — the graph
  finally shows *on* the editing surface, not just beside it.
- Every link mutation's PATCH grows a markdown payload; write count is
  unchanged. Body round-trips happen on link writes now, so the
  documented pill-degradation caveat widens exactly as WP10c recorded —
  mitigated by the no-op guard and the plain-links guidance in the
  profile docstrings.
- Footer text embeds target names at render time; renames stale until the
  next write to the source node. Deliberate (deep links, not pills).
- `fetch_body`, `get_node`, `explore full`, prose excerpts, and both
  migration scripts all read through `body_of`, so none of them ever
  surface footer text to the model.
