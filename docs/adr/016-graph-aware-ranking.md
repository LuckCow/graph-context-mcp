# ADR 016: Edges are relevance evidence — graph-aware ranking

**Status:** Accepted (2026-07-04) — WP11; amends ADR 014 (retrieval flows
through a Ranker, not the vector index directly)

## Context

ADR 014 gave retrieval a recall engine: embedding similarity over node
text. But a bi-encoder can only rank what both vectors happened to
preserve, and a classic cross-encoder reranker can only *reorder* what
recall already found. This project has a signal generic RAG stacks fake
with heuristics: **real, labelled cross-references**. The graph was
structured exactly so that relationships are first-class — ranking is
where that investment pays out beyond traversal.

The motivating failure: "the thing Mira used to break the siege." The
item's summary may share zero vocabulary with the query — semantic recall
never sees it — but it is linked by query-relevant edges to two strong
matches. Only the graph can nominate it.

## Decision

Retrieval is a **`Ranker` application service** — the single entry point
for `find_node`'s semantic tier, resolver suggestions, and the
orchestrator's future RAG prefetch. Its pipeline:

1. **Two recall channels.** Semantic recall seeds ~30 scored candidates
   from the query; **graph expansion** (1–2 hops from the seeds — plus
   the session's focus/recent nodes when the caller is session-aware —
   capped, infra roles excluded) may **recruit** nodes semantic recall
   missed. Recruitment, not just reordering, is the point.
2. **Spreading activation over the candidate subgraph.** Seeds start
   with their semantic scores; 2–3 iterations propagate score along
   edges. The subgraph is ≤~100 nodes regardless of corpus size — the
   propagation cost never scales with the graph, only recall does (and
   that is the vector index's job).
3. **Edge weights are composed, per edge, from:**
   - **Query↔label similarity** — edge labels are text, so the SAME
     embedder scores which relations matter *for this query* ("who was
     at the siege" lights `participated_in`; "where does this run"
     lights `located_at`). One cached vector per relation; no
     hand-maintained table; works for every relation a human invents —
     the space-reflecting philosophy applied to ranking.
   - **Structural priors** — named semantic relations outrank the
     generic `links` mirror (the same subordination `to_edges` applies
     on read); a capture `references`-ing two candidates marks them
     as *about the same thing*; two nodes touched by the same `intent`
     record were *worked on together* (provenance doubles as a task-
     affinity signal).
   - **Degree normalization** — Adamic-Adar-style `1/log(degree)`, so
     a rare connection outweighs one through a 400-member hub. Inert
     at small scale, load-bearing at the scale the graph is meant to
     reach.
4. **Signal weights are profile/mode data** (ADR 015): recency
   (`last_modified`) weighs meaningfully in an assistant's `organizing`
   mode and near zero in fiction — a weight, never a rule. Weights are
   tuned against a small golden eval file of (description → expected
   node) pairs harvested from dogfooding, in the golden-as-review-
   artifact tradition; no vibes-tuning.
5. **Every hit carries its evidence.** Scores are sums of named
   contributions, so the presenter can say *why*: "linked to Mira
   (possesses, strong match); co-referenced by 2 captures." This is
   LLM-facing text in the errors-are-prompts tradition — a model reading
   a resolver suggestion can verify the reasoning before committing a
   mutation target, directly narrowing ADR 014's wrong-node risk.
6. **A cross-encoder reranker is an optional LAST stage** behind a
   `Reranker` port (`GC_RERANKER=off|local|voyage`; local model rides
   the pending container rebuild, Voyage the same allowlist entry as its
   embeddings). Two hard rules keep it honest: it **only reorders**
   (never contributes unexplainable score) and it **never resurrects**
   candidates the fail-closed threshold rejected. It is dogfooding-gated
   with WP11's passage stage, where pair-scoring actually earns its
   cost.

## Consequences

- Ranking degrades gracefully along the deployment ladder: no embedder →
  name search only; embedder → semantic + graph (the graph machinery is
  pure `GraphIndex` computation, free everywhere); reranker → precision
  at the passage level.
- Which channel "leads" is an entry-point property, not a config:
  query-seeded callers are semantic-led; session-seeded callers
  (RAG prefetch) are graph-led. One mechanism, two postures.
- Edge-label vectors join the embedding cache (one per relation —
  dozens, not thousands).
- Hidden coupling made explicit: better graph hygiene (linking at
  creation, good relation labels) now directly improves search. The
  docstrings already push both.
- The eval golden is a new maintained artifact; without it, weight
  changes are unreviewable.
