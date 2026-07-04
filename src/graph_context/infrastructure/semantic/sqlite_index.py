"""SQLite-backed SemanticIndex: the persistent embedding cache (ADR 014).

One file per space; rows are keyed ``(node_id, model)`` so switching
embedders invalidates by construction rather than by migration. Vectors
live as float64 blobs; queries are exact brute-force cosine over an
in-memory mirror loaded lazily -- single-digit milliseconds at the scale
the vector-DB trigger (~100k chunks) guards.

The file is a CACHE: deleting it is always safe (the projector re-embeds
on the next hydrate), which is the whole ADR 014 posture -- persistence
follows cost-to-rebuild, truth stays in Anytype.

sqlite3 is synchronous; operations here are sub-millisecond row touches,
so the async port methods simply run them inline (no thread hop).
"""

from __future__ import annotations

import array
import sqlite3
from collections.abc import Collection
from pathlib import Path

from graph_context.domain.models import NodeId
from graph_context.ports.semantic import Vector

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    node_id      TEXT NOT NULL,
    model        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector       BLOB NOT NULL,
    PRIMARY KEY (node_id, model)
)
"""


class SqliteSemanticIndex:
    """The port over one SQLite file, scoped to one embedder model."""

    def __init__(self, path: str | Path, model: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._model = model
        self._db = sqlite3.connect(str(self._path))
        self._db.execute(_SCHEMA)
        self._db.commit()
        # In-memory mirror for queries; loaded lazily, kept in step by
        # upsert/prune (single-writer per process, like everything else).
        self._cache: dict[NodeId, tuple[str, list[float]]] | None = None

    def close(self) -> None:
        self._db.close()

    async def stored_hash(self, node_id: NodeId) -> str | None:
        entry = self._loaded().get(node_id)
        return entry[0] if entry else None

    async def upsert(
        self, node_id: NodeId, content_hash: str, vector: Vector
    ) -> None:
        values = [float(v) for v in vector]
        self._db.execute(
            "INSERT INTO embeddings (node_id, model, content_hash, vector) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (node_id, model) DO UPDATE "
            "SET content_hash = excluded.content_hash, vector = excluded.vector",
            (node_id, self._model, content_hash, array.array("d", values).tobytes()),
        )
        self._db.commit()
        self._loaded()[node_id] = (content_hash, values)

    async def prune(self, live_ids: Collection[NodeId]) -> None:
        live = set(live_ids)
        cache = self._loaded()
        dead = [node_id for node_id in cache if node_id not in live]
        for node_id in dead:
            del cache[node_id]
        if dead:
            self._db.executemany(
                "DELETE FROM embeddings WHERE node_id = ? AND model = ?",
                [(node_id, self._model) for node_id in dead],
            )
            self._db.commit()

    async def query(
        self, vector: Vector, *, limit: int = 30, threshold: float = 0.0
    ) -> list[tuple[NodeId, float]]:
        query = list(vector)
        scored = [
            (node_id, sum(a * b for a, b in zip(query, stored, strict=True)))
            for node_id, (_, stored) in self._loaded().items()
        ]
        scored = [(node_id, score) for node_id, score in scored if score >= threshold]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    def _loaded(self) -> dict[NodeId, tuple[str, list[float]]]:
        if self._cache is None:
            self._cache = {}
            rows = self._db.execute(
                "SELECT node_id, content_hash, vector FROM embeddings "
                "WHERE model = ?",
                (self._model,),
            )
            for node_id, content_hash, blob in rows:
                values = array.array("d")
                values.frombytes(blob)
                self._cache[node_id] = (content_hash, list(values))
        return self._cache
