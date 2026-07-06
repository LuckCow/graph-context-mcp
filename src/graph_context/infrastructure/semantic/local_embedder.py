"""SentenceTransformerEmbedder: the model-backed local embedder (ADR 014).

The model is baked into the devcontainer image at build time (the egress
firewall forbids downloading it at runtime; ``HF_HUB_OFFLINE=1`` makes the
cache authoritative), so constructing this adapter never touches the
network. Unlike :class:`HashingEmbedder` it understands meaning, not just
shared vocabulary -- "stronghold" lands near "castle" -- which is what the
find_node semantic tier and resolver suggestions were waiting on.

Selected with ``GC_EMBEDDER=local``; ``GC_EMBEDDER_MODEL`` overrides the
model name for images that bake a different one. BGE v1.5 note: queries
and passages are embedded symmetrically (no instruction prefix) -- the
v1.5 models are tuned to make that gap minor, and the Embedder port is
deliberately symmetric.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

# The model the Dockerfile bakes (ADR 014); change both together.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class SentenceTransformerEmbedder:
    """Unit-normalized sentence-transformers embeddings, off the event loop."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        # Import here: torch takes seconds to load and only this adapter
        # needs it -- GC_EMBEDDER=off/hash processes must not pay for it.
        from sentence_transformers import SentenceTransformer

        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        # encode() releases the GIL into native code but interleaved calls
        # thrash; serialize so concurrent embed()s queue instead.
        self._lock = asyncio.Lock()

    @property
    def model_id(self) -> str:
        return self._model_name

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:
            vectors = await asyncio.to_thread(
                self._model.encode, list(texts), normalize_embeddings=True
            )
        return [vector.tolist() for vector in vectors]
