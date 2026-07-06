# ADR 014: Semantic search as a derived projection; embeddings persist as cache

**Status:** Accepted (2026-07-04) — WP11; reaffirms ADR 002 against a
datastore migration; opens WP4's parked "semantic search" item (its entry
criterion — "find the node about X" questions name-search can't answer —
is now met by the product direction). **Amended same day by ADR 016:**
tools do not query the vector index directly — retrieval flows through
the graph-aware `Ranker` (semantic recall + graph expansion/recruitment +
evidence-annotated scoring), with an optional reorder-only cross-encoder
seam behind `GC_RERANKER`.

## Context

Users describe nodes without naming them ("the engineer who reads
stone"), and the orchestrator needs a way to locate relevant world
context for open questions. Name search cannot answer either. The
question that came with the feature: is this the moment to add dedicated
storage — a vector database, or a database replacing hydration outright?

Two facts anchor the answer. First, hydration is a solved problem at
every foreseeable scale: spike S2 measured 2,000 objects in 2–3 API
calls, well under a second; the real space is two orders of magnitude
smaller; and WP1's risk register already names the escape hatch
(persisted index *snapshot* + delta resync) with a ~5k-node trigger that
is nowhere in sight. Second, the system's human-vs-bot coherence rests
entirely on ADR 001/002: Anytype is the single source of truth and every
server-side structure is a derived, disposable projection that may lag
the store but never lead it. A durable database copy of the graph would
have an independent lifetime — and would inherit the hardest problem in
the system (staleness against human edits, including S4's
invisible-to-resync deletions) while saving a cost that rounds to zero.
It would boot stale and need a full reconciliation sweep to be
trustworthy, which *is* hydration with extra steps.

What semantic search actually adds is the first derived artifact that is
**expensive to rebuild**: embeddings. That, not query speed, is the real
storage question.

## Decision

1. **No database replaces hydration.** ADR 002 stands. Written revisit
   triggers, so this is a decision and not a reflex: reopen if a space
   approaches ~5k nodes (cold-start pain — and then as a *snapshot*, per
   WP1), or if WP8+ genuinely needs multi-process access to one graph.
2. **Persistence follows cost-to-rebuild.** The GraphIndex is cheap to
   rebuild and stays ephemeral. Embeddings are costly to recompute and
   earn persistence — **as a cache, never truth**: keyed by
   ``(node id, content hash, embedder model)``, pruned against the live
   id set on hydrate (which reduces S4's deletion problem to cache
   eviction), re-embedded per node when resync sees its content hash
   change. Deleting the cache file is always safe and always converges.
3. **No vector database.** One SQLite file per space (stdlib; zero new
   infrastructure) stores the cache; queries are **exact brute-force
   cosine** in memory — single-digit milliseconds at even 10k chunks, no
   ANN approximation error, nothing to operate. A ``SemanticIndex`` port
   with a contract-tested fake keeps a later swap (sqlite-vec / LanceDB
   at ~100k+ chunks, e.g. heavily chunked prose) adapter-sized.
4. **The embedder is a port with an env selector** (``GC_EMBEDDER``,
   mirroring ``GC_BACKEND``): a local sentence-transformers model (baked
   into the container image — the egress firewall forbids ad-hoc
   downloads) and/or a remote API (Voyage; needs a firewall allowlist
   entry, which the orchestrator's Anthropic driver will need anyway).
   Quality-vs-weight is deferred to dogfooding; the port makes it a
   deployment choice, not an architecture one.
5. **Tool surface: augment, don't multiply** (the WP3 minimalism
   precedent, applied):
   - ``find_node`` grows a third matching tier — exact → substring →
     semantic (threshold-gated) — with semantic hits *labelled* as such;
     the result shape is unchanged (entry-point lines).
   - **Resolution errors become a search surface**: ``NodeNotFound`` from
     the id-or-name resolver appends "closest by meaning" candidates —
     errors are prompts, and this single change serves every node
     parameter of every tool.
   - **Non-feature: no silent fuzzy resolution.** An exact name resolves;
     a semantic match only *suggests* — the model must pick an id. A
     guessed target on ``update_node`` is how the wrong character gets a
     new backstory.
   - A dedicated ``search`` tool is **reserved, dogfooding-gated**, for
     the one job augmentation cannot shape: passage-level retrieval
     (prose/body excerpts anchored to nodes). If the find_node tier plus
     ``include_prose`` covers real usage, it never ships. Orchestrator
     RAG is likely harness-side prefetch, needing no tool at all.
6. **Division of labor is explicit in the docstrings:** semantic search
   finds the door; the graph walks the house. ``explore`` stays purely
   structural; the taught pattern is describe → ``find_node`` →
   ``explore`` from the hit.

## Consequences

- "Find the node I'm describing" works everywhere a node can be named,
  via the resolver's error path — one mechanism, eight tools.
- Hydrate/resync gain an embedding upsert step; content hashing keeps
  resync incremental and idempotent. What to embed starts as
  ``name + summary + reflected fields`` (index-resident), with bodies
  and prose chunks as a follow-on (on-demand reads are unthrottled, S7).
- The container rebuild list grows again (local embedding model and/or
  a firewall allowlist entry) — same rebuild langgraph is waiting on.
- A second cache file exists per space; documented as disposable, like
  every projection.
- The vector-DB question has a written trigger instead of a speculative
  dependency; the datastore-migration question has a written "no" with
  its own triggers.
