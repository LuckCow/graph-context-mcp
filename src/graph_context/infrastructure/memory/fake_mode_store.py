"""In-memory ModeStore: tests and the GC_BACKEND=memory dev mode."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class InMemoryModeStore:
    def __init__(self, payloads: Iterable[dict[str, Any]] = ()) -> None:
        self._payloads = [dict(p) for p in payloads]

    async def load(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payloads]

    def set_payloads(self, payloads: Iterable[dict[str, Any]]) -> None:
        """Test convenience: the space's mode objects changed."""
        self._payloads = [dict(p) for p in payloads]
