"""Ports for the semantic layer (ADR 014): embedder + vector cache.

Both are DERIVED-projection machinery: the index is a cache keyed by
``(node id, content hash)`` per embedder model, pruned against the live id
set on hydrate, and always safe to delete -- Anytype stays the only truth.

``Embedder.model_id`` participates in cache keying: a model change makes
every stored vector stale by construction (the projector simply sees
missing hashes and re-embeds).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from graph_context.domain.models import NodeId

Vector = Sequence[float]


class Embedder(Protocol):
    """Text -> unit-normalized vectors. Deployment-selected (GC_EMBEDDER)."""

    @property
    def model_id(self) -> str:
        """Cache-key component; changing models invalidates the cache."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SemanticIndex(Protocol):
    """The embedding cache + exact-similarity query (one model per index).

    Implementations hold unit vectors and return cosine scores (dot
    products) in descending order. ``prune`` is the S4 deletion answer:
    ids absent from the live set are evicted.
    """

    async def stored_hash(self, node_id: NodeId) -> str | None:
        """The content hash last embedded for this node, or ``None``."""
        ...

    async def upsert(
        self, node_id: NodeId, content_hash: str, vector: Vector
    ) -> None: ...

    async def prune(self, live_ids: Collection[NodeId]) -> None: ...

    async def query(
        self, vector: Vector, *, limit: int = 30, threshold: float = 0.0
    ) -> list[tuple[NodeId, float]]:
        """Best matches by cosine, best first; below-threshold hits dropped
        (fail closed -- an honest empty beats confident noise)."""
        ...
