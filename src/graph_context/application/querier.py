"""Use-case: attribute queries across the graph (the ``query`` tool).

The pure engine lives in :mod:`graph_context.domain.query`. Two entry
points: ad-hoc queries (parameters straight off the tool surface) and
SAVED VIEWS (WP13/ADR 018) -- the user's Anytype Set views, compiled by
the :class:`ViewCatalog` into the same ``NodeQuery`` shape and run on
the same engine, so a view edited in the desktop applies on the next
call with the store's own meaning.

Unlike :class:`Explorer`, no session dependency: a corpus scan has no
start node to default from the focus stack, and listing the world must
not mutate the working set.
"""

from __future__ import annotations

from dataclasses import replace

from graph_context.domain.query import NodeQuery, QueryResult, run_query
from graph_context.domain.schema import Role
from graph_context.errors import GraphContextError
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.view_catalog import SavedView, ViewCatalog


class Querier:
    """Corpus-wide (or neighborhood-anchored) attribute queries."""

    def __init__(
        self, repository: GraphRepository, views: ViewCatalog | None = None
    ) -> None:
        self._repository = repository
        self._views = views

    async def query(self, node_query: NodeQuery) -> QueryResult:
        return run_query(self._repository.graph, node_query)

    async def run_view(
        self,
        name: str,
        *,
        limit: int,
        exclude_roles: frozenset[Role],
    ) -> tuple[SavedView, QueryResult]:
        """Resolve a saved view by name and run its compiled query.

        ``name`` matches the set name, the view name, or ``set/view``
        (case-insensitive). Misses and ambiguity list what IS runnable --
        the error is a prompt (the catalog already excludes views that
        could not be compiled, e.g. sets with no source configured).
        """
        views = await self._views.load() if self._views is not None else ()
        wanted = name.strip().casefold()
        matches = [
            v for v in views
            if wanted in {
                v.set_name.casefold(), v.view_name.casefold(),
                v.full_name.casefold(),
            }
        ]
        listing = ", ".join(sorted(v.full_name for v in views)) or "(none)"
        if not matches:
            raise GraphContextError(
                f"no saved view matches {name!r}; runnable views: {listing}. "
                "A set only appears here once its source is configured in "
                "Anytype and it holds at least one object."
            )
        if len(matches) > 1:
            options = ", ".join(sorted(v.full_name for v in matches))
            raise GraphContextError(
                f"{name!r} matches {len(matches)} views: {options}. "
                "Use the set/view form to pick one."
            )
        saved = matches[0]
        node_query = replace(
            saved.query, limit=limit, exclude_roles=exclude_roles
        )
        return saved, run_query(self._repository.graph, node_query)
