"""In-memory ``ViewCatalog``: tests and the memory backend.

Holds :class:`SavedView` values carrying REAL ``NodeQuery`` objects, so
fake-served views run through the same ``run_query`` engine as compiled
live views -- the fake and the adapter share semantics by construction
(the fakes-are-contracts rule applied to views).
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_context.ports.view_catalog import SavedView


class InMemoryViewCatalog:
    def __init__(self, views: Sequence[SavedView] = ()) -> None:
        self._views = tuple(views)

    async def load(self) -> tuple[SavedView, ...]:
        return self._views
