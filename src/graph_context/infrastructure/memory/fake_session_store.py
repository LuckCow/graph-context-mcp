"""In-memory SessionStore: tests and the GC_BACKEND=memory dev mode."""

from __future__ import annotations

from typing import Any


class InMemorySessionStore:
    def __init__(self) -> None:
        self._snapshot: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any] | None:
        return self._snapshot

    async def save(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
