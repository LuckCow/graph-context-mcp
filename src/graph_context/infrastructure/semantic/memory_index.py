"""In-memory SemanticIndex: the executable spec, and the memory backend's."""

from __future__ import annotations

from collections.abc import Collection

from graph_context.domain.models import NodeId
from graph_context.ports.semantic import Vector


class InMemorySemanticIndex:
    """Vectors in a dict; exact cosine (dot of unit vectors) on query."""

    def __init__(self) -> None:
        self._entries: dict[NodeId, tuple[str, list[float]]] = {}

    async def stored_hash(self, node_id: NodeId) -> str | None:
        entry = self._entries.get(node_id)
        return entry[0] if entry else None

    async def upsert(
        self, node_id: NodeId, content_hash: str, vector: Vector
    ) -> None:
        self._entries[node_id] = (content_hash, list(vector))

    async def prune(self, live_ids: Collection[NodeId]) -> None:
        live = set(live_ids)
        for node_id in list(self._entries):
            if node_id not in live:
                del self._entries[node_id]

    async def query(
        self, vector: Vector, *, limit: int = 30, threshold: float = 0.0
    ) -> list[tuple[NodeId, float]]:
        query = list(vector)
        scored = [
            (node_id, sum(a * b for a, b in zip(query, stored, strict=True)))
            for node_id, (_, stored) in self._entries.items()
        ]
        scored = [(node_id, score) for node_id, score in scored if score >= threshold]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]
