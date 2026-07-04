"""HashingEmbedder: deterministic bag-of-words vectors, no model, no I/O.

The feature-hashing trick: each token hashes to a dimension and a sign;
texts sharing vocabulary land near each other. Weak on synonyms (it is
word overlap, not meaning), which is exactly why it is honest as the
pre-rebuild default OFF and invaluable in tests: similarity is stable
across runs and machines, so ranking tests can assert on ordering.

Selected with ``GC_EMBEDDER=hash`` (demos, offline dev); the golden eval
file runs against it in CI, so eval queries are written for overlap.
"""

from __future__ import annotations

import math
import re
import zlib
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    """Feature-hashed unigram embeddings, unit-normalized."""

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"hash-v1-{self._dim}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in _TOKEN.findall(text.lower()):
            h = zlib.crc32(token.encode())
            index = h % self._dim
            sign = 1.0 if (h >> 16) & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]
