"""Ranker: retrieval where edges are relevance evidence (ADR 016).

The single retrieval entry for the tool layer (and, later, orchestrator
RAG prefetch). Pipeline:

    semantic recall (seeds, scored)  ─┐
    graph expansion (1 hop + infra    ├─▶ spreading activation over the
    look-through; may RECRUIT nodes  ─┘   candidate subgraph (bounded,
    recall never saw)                     never scales with the corpus)
                                      ─▶ fail-closed threshold
                                      ─▶ top N with EVIDENCE strings

Edge conduction composes three factors per edge:

* **query↔label similarity** -- edge labels are text; the same embedder
  scores which relations matter for THIS query (cached, one vector per
  label). A floor keeps unrelated labels conducting a little: structure
  is evidence even when the label is not.
* **structural priors** -- named relations > the generic ``links``
  mirror; two candidates sharing a capture (``references``) are about
  the same thing; sharing an intent record means worked-on-together.
* **degree normalization** -- Adamic-Adar style: a connection through a
  400-member hub conducts far less than a rare one.

Signal weights are data (:class:`RankingWeights`, profile-supplied per
ADR 015) and answer to the golden eval file, not vibes. Scores are sums
of NAMED contributions, so every hit explains itself -- the
errors-are-prompts discipline applied to ranking.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import INFRA_ROLES, Role
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.semantic import Embedder, SemanticIndex


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """Tunable ranking signals -- profile/mode data (ADR 015/016)."""

    recall_k: int = 30
    seed_threshold: float = 0.20    # recall gate (fail closed)
    final_threshold: float = 0.05   # output gate (fail closed)
    expansion_cap: int = 100        # max candidate-subgraph size
    propagation: float = 0.6        # damping per hop
    iterations: int = 2
    label_floor: float = 0.35       # structure conducts even off-topic labels
    links_mirror_prior: float = 0.3
    capture_coreference_prior: float = 0.6
    intent_cotouch_prior: float = 0.4
    session_bonus: float = 0.25     # energy injected into session seeds
    recency: float = 0.0            # assistant raises this; fiction leaves 0
    # Look-through fanout guard: an infra connector touching more nodes
    # than this is a hub of bookkeeping, not evidence.
    connector_fanout_cap: int = 20


@dataclass(frozen=True, slots=True)
class RankedHit:
    node: Node
    score: float
    evidence: tuple[str, ...]


@dataclass(slots=True)
class _Contribution:
    source: NodeId
    amount: float
    description: str


class Ranker:
    """Semantic recall + graph conduction + evidence."""

    def __init__(
        self,
        repository: GraphRepository,
        embedder: Embedder,
        index: SemanticIndex,
        weights: RankingWeights | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._index = index
        self._weights = weights or RankingWeights()
        self._label_vectors: dict[str, list[float]] = {}

    async def rank(
        self,
        query: str,
        *,
        limit: int = 5,
        session_seeds: Sequence[NodeId] = (),
    ) -> list[RankedHit]:
        w = self._weights
        graph = self._repository.graph
        [query_vector] = await self._embedder.embed([query])

        # 1. Semantic seeds (stale cache ids and infra roles dropped).
        seeds: dict[NodeId, float] = {}
        for node_id, score in await self._index.query(
            query_vector, limit=w.recall_k, threshold=w.seed_threshold
        ):
            if graph.has_node(node_id) and graph.node(node_id).role not in INFRA_ROLES:
                seeds[node_id] = score
        for node_id in session_seeds:
            if graph.has_node(node_id) and graph.node(node_id).role not in INFRA_ROLES:
                seeds[node_id] = seeds.get(node_id, 0.0) + w.session_bonus
        if not seeds:
            return []

        # 2. Candidate subgraph: seeds + 1-hop recruits + infra look-through
        # pairs, capped. conduits: (from, to, weight-parts, description).
        candidates = dict(seeds)  # id -> base energy (0.0 for recruits)
        conduits: list[tuple[NodeId, NodeId, float, str]] = []
        ordered_seeds = sorted(seeds, key=lambda i: -seeds[i])
        for seed_id in ordered_seeds:
            for edge, neighbor in graph.neighbors(seed_id):
                if neighbor.role in INFRA_ROLES:
                    for conduit in self._look_through(seed_id, edge, neighbor):
                        target = conduit[1]
                        if target not in candidates:
                            if len(candidates) >= w.expansion_cap:
                                continue
                            candidates[target] = 0.0
                        conduits.append(conduit)
                    continue
                if neighbor.id not in candidates:
                    if len(candidates) >= w.expansion_cap:
                        continue
                    candidates[neighbor.id] = 0.0
                weight = await self._edge_weight(query_vector, edge.type, neighbor.id)
                label_note = await self._label_note(query_vector, edge.type)
                conduits.append((
                    seed_id, neighbor.id, weight,
                    f"linked to {graph.node(seed_id).name} via {edge.type}"
                    f"{label_note}",
                ))

        # 3. Spreading activation with per-node contribution bookkeeping.
        scores: dict[NodeId, float] = dict(candidates)
        contributions: dict[NodeId, list[_Contribution]] = {c: [] for c in candidates}
        for _ in range(w.iterations):
            incoming: dict[NodeId, float] = {}
            for source, target, weight, description in conduits:
                if target not in candidates:
                    continue
                amount = w.propagation * weight * scores.get(source, 0.0)
                if amount <= 0.0:
                    continue
                incoming[target] = incoming.get(target, 0.0) + amount
                contributions[target].append(
                    _Contribution(source, amount, description)
                )
            scores = {
                node_id: candidates[node_id] + incoming.get(node_id, 0.0)
                for node_id in candidates
            }

        # 4. Recency bonus (profile-weighted; percentile over candidates).
        if w.recency > 0:
            stamped = sorted(
                (node_id for node_id in candidates
                 if graph.node(node_id).modified_at),
                key=lambda i: graph.node(i).modified_at,
            )
            for position, node_id in enumerate(stamped):
                bonus = (
                    w.recency * position / (len(stamped) - 1)
                    if len(stamped) > 1 else w.recency
                )
                if bonus > 0:
                    scores[node_id] += bonus
                    contributions[node_id].append(
                        _Contribution(node_id, bonus, "recently modified")
                    )

        # 5. Fail-closed output. Recruits carry no seed energy, so anything
        # surviving here earned it through the graph.
        hits = [
            RankedHit(
                node=graph.node(node_id),
                score=score,
                evidence=self._evidence(node_id, seeds, contributions[node_id]),
            )
            for node_id, score in scores.items()
            if score >= w.final_threshold
        ]
        hits.sort(key=lambda h: (-h.score, h.node.name.lower()))
        return hits[:limit]

    # -- internals -----------------------------------------------------

    def _look_through(
        self, origin: NodeId, edge_in: Edge, infra: Node
    ) -> list[tuple[NodeId, NodeId, float, str]]:
        """Candidate—infra—candidate pairs: captures/intents as connectors.

        The infra node never becomes a candidate; it conducts. Hub
        bookkeeping (an intent that touched everything) is capped out.
        """
        w = self._weights
        graph = self._repository.graph
        if infra.role is Role.CAPTURE:
            prior, phrasing = w.capture_coreference_prior, "co-referenced with"
        elif infra.role is Role.INTENT:
            prior, phrasing = w.intent_cotouch_prior, "worked on together with"
        else:
            return []
        others = [
            neighbor for _, neighbor in graph.neighbors(infra.id)
            if neighbor.id != origin and neighbor.role not in INFRA_ROLES
        ]
        if not others or len(others) > w.connector_fanout_cap:
            return []
        degree_norm = 1.0 / math.log2(2 + len(others))
        origin_name = graph.node(origin).name
        return [
            (
                origin, other.id, prior * degree_norm,
                f"{phrasing} {origin_name} ({infra.name!r})",
            )
            for other in others
        ]

    async def _edge_weight(
        self, query_vector: Sequence[float], label: str, target: NodeId
    ) -> float:
        w = self._weights
        similarity = max(0.0, await self._label_similarity(query_vector, label))
        conduction = w.label_floor + (1.0 - w.label_floor) * similarity
        prior = w.links_mirror_prior if label == "links" else 1.0
        degree = sum(1 for _ in self._repository.graph.edges(target))
        return conduction * prior * (1.0 / math.log2(2 + degree))

    async def _label_similarity(
        self, query_vector: Sequence[float], label: str
    ) -> float:
        if label not in self._label_vectors:
            [vector] = await self._embedder.embed([label.replace("_", " ")])
            self._label_vectors[label] = vector
        stored = self._label_vectors[label]
        return sum(a * b for a, b in zip(query_vector, stored, strict=True))

    async def _label_note(
        self, query_vector: Sequence[float], label: str
    ) -> str:
        similarity = await self._label_similarity(query_vector, label)
        return " (query-relevant relation)" if similarity > 0.35 else ""

    def _evidence(
        self,
        node_id: NodeId,
        seeds: dict[NodeId, float],
        incoming: list[_Contribution],
    ) -> tuple[str, ...]:
        parts: list[str] = []
        if node_id in seeds:
            parts.append(f"matched the description ({seeds[node_id]:.2f})")
        strongest = sorted(incoming, key=lambda c: -c.amount)
        seen: set[str] = set()
        for contribution in strongest:
            if contribution.description in seen:
                continue
            seen.add(contribution.description)
            parts.append(contribution.description)
            if len(parts) >= 3:
                break
        return tuple(parts)
