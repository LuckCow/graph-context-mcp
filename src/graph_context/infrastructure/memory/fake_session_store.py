"""In-memory SessionStore: tests and the GC_BACKEND=memory dev mode."""

from __future__ import annotations

from typing import Any

from graph_context.ports.session_store import require_session_key


class InMemorySessionStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        return self._snapshots.get(require_session_key(key))

    async def save(self, snapshot: dict[str, Any], key: str) -> None:
        self._snapshots[require_session_key(key)] = snapshot
