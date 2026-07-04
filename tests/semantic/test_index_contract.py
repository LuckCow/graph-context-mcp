"""The SemanticIndex contract: one behavioral spec, every implementation."""

from __future__ import annotations

import math

import pytest

from graph_context.infrastructure.semantic.hashing_embedder import HashingEmbedder
from graph_context.infrastructure.semantic.memory_index import InMemorySemanticIndex
from graph_context.infrastructure.semantic.sqlite_index import SqliteSemanticIndex


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


class SemanticIndexContract:
    """Inherit + provide an `index` fixture to certify an implementation."""

    async def test_upsert_query_orders_by_cosine(self, index):
        await index.upsert("east", "h1", _unit(1, 0))
        await index.upsert("north", "h2", _unit(0, 1))
        await index.upsert("northeast", "h3", _unit(1, 1))
        hits = await index.query(_unit(1, 0.1))
        assert [node_id for node_id, _ in hits] == ["east", "northeast", "north"]

    async def test_threshold_fails_closed(self, index):
        await index.upsert("east", "h1", _unit(1, 0))
        await index.upsert("north", "h2", _unit(0, 1))
        hits = await index.query(_unit(1, 0), threshold=0.5)
        assert [node_id for node_id, _ in hits] == ["east"]

    async def test_limit_caps_results(self, index):
        for i in range(5):
            await index.upsert(f"n{i}", "h", _unit(1, i / 10))
        assert len(await index.query(_unit(1, 0), limit=3)) == 3

    async def test_stored_hash_supports_skip_logic(self, index):
        assert await index.stored_hash("east") is None
        await index.upsert("east", "h1", _unit(1, 0))
        assert await index.stored_hash("east") == "h1"
        await index.upsert("east", "h2", _unit(0, 1))  # re-embed replaces
        assert await index.stored_hash("east") == "h2"
        hits = await index.query(_unit(0, 1))
        assert hits[0][0] == "east" and hits[0][1] == pytest.approx(1.0)

    async def test_prune_evicts_dead_ids(self, index):
        await index.upsert("keep", "h", _unit(1, 0))
        await index.upsert("dead", "h", _unit(0, 1))
        await index.prune(["keep"])
        assert await index.stored_hash("dead") is None
        assert [n for n, _ in await index.query(_unit(0, 1))] == ["keep"]


class TestInMemoryIndex(SemanticIndexContract):
    @pytest.fixture
    def index(self):
        return InMemorySemanticIndex()


class TestSqliteIndex(SemanticIndexContract):
    @pytest.fixture
    def index(self, tmp_path):
        return SqliteSemanticIndex(tmp_path / "cache.sqlite", model="test-model")

    async def test_cache_survives_a_restart(self, tmp_path):
        path = tmp_path / "cache.sqlite"
        first = SqliteSemanticIndex(path, model="m")
        await first.upsert("east", "h1", _unit(1, 0))
        first.close()
        second = SqliteSemanticIndex(path, model="m")
        assert await second.stored_hash("east") == "h1"
        assert [n for n, _ in await second.query(_unit(1, 0))] == ["east"]
        second.close()

    async def test_models_are_isolated_in_one_file(self, tmp_path):
        path = tmp_path / "cache.sqlite"
        old = SqliteSemanticIndex(path, model="old-model")
        await old.upsert("east", "h1", _unit(1, 0))
        new = SqliteSemanticIndex(path, model="new-model")
        # A model switch invalidates by construction: nothing is visible.
        assert await new.stored_hash("east") is None
        assert await new.query(_unit(1, 0)) == []
        old.close()
        new.close()


class TestHashingEmbedder:
    async def test_deterministic_and_unit_normalized(self):
        embedder = HashingEmbedder()
        [a], [b] = (
            await embedder.embed(["the siege engineer"]),
            await embedder.embed(["the siege engineer"]),
        )
        assert a == b
        assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)

    async def test_shared_vocabulary_scores_higher(self):
        embedder = HashingEmbedder()
        vectors = await embedder.embed([
            "exiled siege engineer of brakk",
            "an engineer who survived the siege",
            "a merchant selling fruit",
        ])
        def dot(x: list[float], y: list[float]) -> float:
            return sum(a * b for a, b in zip(x, y, strict=True))

        assert dot(vectors[0], vectors[1]) > dot(vectors[0], vectors[2])

    async def test_empty_text_is_a_zero_vector(self):
        embedder = HashingEmbedder()
        [v] = await embedder.embed(["!!!"])
        assert all(x == 0.0 for x in v)
