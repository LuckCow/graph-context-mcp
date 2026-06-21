"""Use-case: graph exploration (the ``explore`` and ``find_path`` tools).

The pure traversal engines live in the domain; this service adds the
session-aware behaviour the tools promise:

    * an omitted start node defaults to the top of the focus stack
      (raising :class:`EmptyFocusStack` with an actionable message when
      there is nothing to default to);
    * the start node is pushed onto the focus stack, so successive
      explorations naturally walk the working set forward.

Scene assembly is not a separate tool: it is an ``ExploreQuery``
configuration (start at an Event, depth 1-2, include Characters /
Locations / Items). Keeping that as a calling convention rather than code
is deliberate -- see the proposal's "small, parameterized tool surface".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from graph_context.domain import pathfinding, traversal
from graph_context.domain.models import NodeId
from graph_context.domain.pathfinding import Path
from graph_context.domain.schema import EdgeType
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery, ExploreResult
from graph_context.errors import EmptyFocusStack
from graph_context.ports.graph_repository import GraphRepository


class Explorer:
    """Session-aware reads over the story-world graph."""

    def __init__(self, repository: GraphRepository, session: SessionState) -> None:
        self._repository = repository
        self._session = session

    async def explore(self, query: ExploreQuery) -> ExploreResult:
        """Run a bounded traversal; empty ``query.start`` uses the focus top."""
        query = replace(query, start=self._resolve_start(query.start))
        result = traversal.explore(self._repository.graph, query)
        self._session.touch(query.start)
        return result

    async def find_path(
        self,
        source: NodeId | None,
        target: NodeId,
        *,
        edge_types: Iterable[EdgeType] | None = None,
        max_length: int = pathfinding.DEFAULT_MAX_LENGTH,
    ) -> Path | None:
        """Shortest meaningful path; ``source=None`` uses the focus top."""
        resolved = self._resolve_start(source)
        path = pathfinding.find_path(
            self._repository.graph,
            resolved,
            target,
            edge_types=edge_types,
            max_length=max_length,
        )
        self._session.touch(resolved)
        return path

    def _resolve_start(self, start: NodeId | None) -> NodeId:
        if start:
            return start
        top = self._session.focus.top
        if top is None:
            raise EmptyFocusStack()
        return top
