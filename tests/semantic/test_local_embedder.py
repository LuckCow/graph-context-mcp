"""SentenceTransformerEmbedder against the baked model (ADR 014).

Self-skips when the model is absent from the local HF cache: CI installs
sentence-transformers but never downloads the model; the devcontainer
image bakes it (Dockerfile), so these run everywhere the `local` embedder
can actually be selected.
"""

from __future__ import annotations

import math

import pytest

from graph_context.infrastructure.semantic.local_embedder import (
    DEFAULT_MODEL,
    SentenceTransformerEmbedder,
)


def model_cached() -> bool:
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(DEFAULT_MODEL, local_files_only=True)
    except Exception:  # noqa: BLE001 - any miss means "not baked here"
        return False
    return True


pytestmark = pytest.mark.skipif(
    not model_cached(), reason=f"{DEFAULT_MODEL} not in the local HF cache"
)


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder()  # loads the model once per module


def _dot(x: list[float], y: list[float]) -> float:
    return sum(a * b for a, b in zip(x, y, strict=True))


class TestSentenceTransformerEmbedder:
    async def test_deterministic_and_unit_normalized(self, embedder):
        [a], [b] = (
            await embedder.embed(["the siege engineer"]),
            await embedder.embed(["the siege engineer"]),
        )
        assert a == b
        assert math.isclose(_dot(a, a), 1.0, rel_tol=1e-5)

    async def test_meaning_beats_vocabulary(self, embedder):
        """The reason this adapter exists: synonyms score without any
        shared token, which the hashing embedder cannot do."""
        vectors = await embedder.embed([
            "an ancient fortified castle",
            "a stronghold with high walls",
            "a merchant selling fruit",
        ])
        assert _dot(vectors[0], vectors[1]) > _dot(vectors[0], vectors[2])

    async def test_model_id_keys_the_cache_by_model_name(self, embedder):
        assert embedder.model_id == DEFAULT_MODEL

    async def test_empty_batch_embeds_to_nothing(self, embedder):
        assert await embedder.embed([]) == []
