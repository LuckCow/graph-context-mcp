"""Use-case: attribute queries across the graph (the ``query`` tool).

The pure engine lives in :mod:`graph_context.domain.query`; this service
is deliberately thin today. It exists as the seam where saved-view
resolution lands (ADR 018 fast-follow: a named Anytype Set view compiles
to a :class:`NodeQuery` and runs through the same engine).

Unlike :class:`Explorer`, no session dependency: a corpus scan has no
start node to default from the focus stack, and listing the world must
not mutate the working set.
"""

from __future__ import annotations

from graph_context.domain.query import NodeQuery, QueryResult, run_query
from graph_context.ports.graph_repository import GraphRepository


class Querier:
    """Corpus-wide (or neighborhood-anchored) attribute queries."""

    def __init__(self, repository: GraphRepository) -> None:
        self._repository = repository

    async def query(self, node_query: NodeQuery) -> QueryResult:
        return run_query(self._repository.graph, node_query)
