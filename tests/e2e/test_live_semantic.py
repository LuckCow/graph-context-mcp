"""Live semantic-cache behavior (WP11 / ADR 014) against a real server.

Pins the two claims the mock-backed suite cannot: the SQLite embedding
cache survives a process restart without re-embedding unchanged nodes,
and a human's out-of-band rename re-embeds exactly the renamed node on
resync. Runs under the real local embedder (the WP11 deferral gated this
run on one existing) -- self-skips where the model isn't baked, on top of
the usual ``ANYTYPE_E2E`` gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from graph_context.application.semantic_projector import SemanticProjector
from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.semantic.sqlite_index import SqliteSemanticIndex
from graph_context.ports.semantic import Embedder
from tests.semantic.test_local_embedder import DEFAULT_MODEL, model_cached


class CountingEmbedder:
    """Wraps the real embedder; records every text it is asked to embed,
    so "nothing was re-embedded" is asserted on calls, not inferred."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.embedded: list[str] = []

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return await self._inner.embed(texts)


@pytest.fixture(scope="module")
def local_embedder() -> Embedder:
    if not model_cached():
        pytest.skip(f"{DEFAULT_MODEL} not in the local HF cache")
    from graph_context.infrastructure.semantic.local_embedder import (
        SentenceTransformerEmbedder,
    )

    return SentenceTransformerEmbedder()


async def test_cache_survives_restart_and_rename_re_embeds(
    repo, raw_api, local_embedder, tmp_path
):
    node = await repo.create_node(
        NodeDraft("Character", name="Mira", summary="An exiled siege engineer.")
    )
    cache_path = tmp_path / "semantic.sqlite"

    # First "process": full pass embeds the node into the persistent cache.
    first = CountingEmbedder(local_embedder)
    index = SqliteSemanticIndex(cache_path, model=first.model_id)
    assert await SemanticProjector(repo, first, index).refresh() >= 1
    assert any("Mira" in text for text in first.embedded)
    index.close()

    # "Restart": a fresh index handle over the same file. The stored hashes
    # match the unchanged graph, so the full pass embeds NOTHING.
    second = CountingEmbedder(local_embedder)
    index = SqliteSemanticIndex(cache_path, model=second.model_id)
    projector = SemanticProjector(repo, second, index)
    assert await projector.refresh() == 0
    assert second.embedded == []

    # A human renames the node out of band (in a later second -- spike S3:
    # last_modified_date is second-granular). Resync reports it; the
    # incremental refresh re-embeds exactly that node.
    await asyncio.sleep(1.5)
    raw_api.rename(node.id, "Mira the Returned")
    changed = await repo.resync()
    assert node.id in changed
    assert await projector.refresh(changed) == 1
    assert second.embedded and all("Mira the Returned" in t for t in second.embedded)
    index.close()
