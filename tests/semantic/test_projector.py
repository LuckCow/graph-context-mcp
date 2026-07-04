"""SemanticProjector: hash-keyed idempotency, incremental refresh, pruning."""

from __future__ import annotations

from collections.abc import Sequence

from graph_context.application.semantic_projector import (
    SemanticProjector,
    content_hash,
    corpus_text,
)
from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.infrastructure.semantic.hashing_embedder import HashingEmbedder
from graph_context.infrastructure.semantic.memory_index import InMemorySemanticIndex


class CountingEmbedder(HashingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.embedded: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return await super().embed(texts)


async def _world() -> tuple[InMemoryGraphRepository, CountingEmbedder,
                            InMemorySemanticIndex, SemanticProjector]:
    repository = InMemoryGraphRepository()
    await repository.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer.",
        fields={"role": "protagonist"},
    ))
    await repository.create_node(NodeDraft(
        "Location", name="The Undercroft", summary="Vaults beneath Brakk.",
    ))
    await repository.create_node(NodeDraft(
        "gc_prose", name="Scene", summary="s", body="ash",  # infra: never embedded
    ))
    embedder = CountingEmbedder()
    index = InMemorySemanticIndex()
    return repository, embedder, index, SemanticProjector(repository, embedder, index)


async def test_full_refresh_embeds_story_nodes_only_and_is_idempotent() -> None:
    repository, embedder, index, projector = await _world()
    assert await projector.refresh() == 2  # Mira + Undercroft, not the capture
    assert len(embedder.embedded) == 2
    assert await projector.refresh() == 0  # hashes match: nothing re-embedded
    assert len(embedder.embedded) == 2


async def test_incremental_refresh_touches_only_changed_nodes() -> None:
    repository, embedder, index, projector = await _world()
    await projector.refresh()
    mira = repository.graph.resolve("Mira")
    await repository.update_node(mira.id, summary="Leads the survivors now.")
    assert await projector.refresh([mira.id]) == 1
    assert embedder.embedded[-1].startswith("Mira")
    assert "Leads the survivors" in embedder.embedded[-1]


async def test_full_refresh_prunes_deleted_nodes() -> None:
    repository, embedder, index, projector = await _world()
    await projector.refresh()
    mira = repository.graph.resolve("Mira")
    repository.graph.remove_node(mira.id)
    await projector.refresh()
    assert await index.stored_hash(mira.id) is None


async def test_corpus_excludes_recency_but_includes_fields() -> None:
    from dataclasses import replace

    repository, _, _, _ = await _world()
    mira = repository.graph.resolve("Mira")
    text = corpus_text(mira)
    assert "role: protagonist" in text and "Exiled siege engineer." in text
    # modified_at must never enter the hash: recency is a signal, not
    # content -- otherwise every store touch would force a re-embed.
    stamped = replace(mira, modified_at="2026-07-04T12:00:00Z")
    assert content_hash(corpus_text(stamped)) == content_hash(text)


async def test_resync_ids_for_vanished_nodes_are_skipped() -> None:
    repository, _, _, projector = await _world()
    await projector.refresh()
    assert await projector.refresh(["no-such-node"]) == 0
