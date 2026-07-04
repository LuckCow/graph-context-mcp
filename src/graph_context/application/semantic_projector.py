"""SemanticProjector: keeps the embedding cache in step with the graph.

The third projection (ADR 014): like the GraphIndex it is derived and
disposable, but embeddings are expensive to rebuild, so this service
maintains the persistent cache instead of recomputing from scratch:

* ``refresh()``            -- full pass (after hydrate): embed every node
                              whose content hash changed, prune ids that
                              left the live set (the S4 deletion answer).
* ``refresh(changed_ids)`` -- incremental (after resync): touch only the
                              reported nodes.

The corpus per node is ``name + summary + reflected fields`` -- the
index-resident text (bodies/passages are WP11 stage 2). ``modified_at``
is deliberately NOT part of the corpus: recency is a ranking signal, not
content, and hashing it would force a re-embed on every touch.

Infra-role nodes (captures, session context, intent records) are not
embedded: node search finds world entities; passage search over captures
is the stage-2 question.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Collection

from graph_context.domain.models import Node, NodeId
from graph_context.domain.schema import INFRA_ROLES
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.semantic import Embedder, SemanticIndex

logger = logging.getLogger(__name__)


def corpus_text(node: Node) -> str:
    """The embedded text for one node: what a describer would describe."""
    lines = [node.name, node.type, node.summary]
    lines.extend(f"{key}: {value}" for key, value in sorted(node.fields.items()))
    return "\n".join(line for line in lines if line)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SemanticProjector:
    """Graph -> embedding cache synchronization."""

    def __init__(
        self,
        repository: GraphRepository,
        embedder: Embedder,
        index: SemanticIndex,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._index = index

    async def refresh(self, changed: Collection[NodeId] | None = None) -> int:
        """Bring the cache up to date; returns how many nodes were embedded.

        ``None`` means a full pass with pruning; a changed-id set (from
        resync) touches only those nodes and skips the prune -- deletions
        are only visible to full reconciliation anyway (S4).
        """
        graph = self._repository.graph
        if changed is None:
            nodes = [n for n in graph.nodes() if n.role not in INFRA_ROLES]
        else:
            nodes = [
                graph.node(node_id) for node_id in changed if graph.has_node(node_id)
            ]
            nodes = [n for n in nodes if n.role not in INFRA_ROLES]

        stale: list[tuple[NodeId, str, str]] = []  # (id, hash, text)
        for node in nodes:
            text = corpus_text(node)
            digest = content_hash(text)
            if await self._index.stored_hash(node.id) != digest:
                stale.append((node.id, digest, text))
        if stale:
            vectors = await self._embedder.embed([text for _, _, text in stale])
            for (node_id, digest, _), vector in zip(stale, vectors, strict=True):
                await self._index.upsert(node_id, digest, vector)
        if changed is None:
            await self._index.prune([
                n.id for n in graph.nodes() if n.role not in INFRA_ROLES
            ])
        if stale:
            logger.info("semantic projector embedded %d node(s)", len(stale))
        return len(stale)
