"""Use-case: graph exploration (the ``explore`` and ``find_path`` tools).

The pure traversal engines live in the domain; this service adds the
session-aware behaviour the tools promise:

    * an omitted start node defaults to the session's working-set top,
      falling back to the most recently touched node (raising
      :class:`NoDefaultStart` with an actionable message when there is
      nothing to default to);
    * the start node is touched, so it lands in recent history -- the
      working set itself is curated only by explicit ``hold`` calls (WP15).

Scene assembly is not a separate tool: it is an ``ExploreQuery``
configuration (start at an Event, depth 1-2, include Characters /
Locations / Items). Keeping that as a calling convention rather than code
is deliberate -- see the proposal's "small, parameterized tool surface".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import replace

from graph_context.domain import pathfinding, traversal
from graph_context.domain.models import NodeId
from graph_context.domain.pathfinding import Path
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery, ExploreResult
from graph_context.errors import NoDefaultStart
from graph_context.ports.graph_repository import GraphRepository


class Explorer:
    """Session-aware reads over the story-world graph."""

    def __init__(self, repository: GraphRepository, session: SessionState) -> None:
        self._repository = repository
        self._session = session

    async def explore(self, query: ExploreQuery) -> ExploreResult:
        """Run a bounded traversal; empty ``query.start`` uses the session default."""
        query = replace(query, start=self._resolve_start(query.start))
        result = traversal.explore(self._repository.graph, query)
        self._session.touch(query.start)
        return result

    async def bodies_for(self, node_ids: Sequence[NodeId]) -> dict[NodeId, str]:
        """Fan out on-demand body fetches -- ``explore detail='full'`` (ADR 010).

        Bodies never enter the index (A7: list/search responses omit them),
        so full-text scene assembly is one concurrent GET per hit. Reads are
        unthrottled (S7) and the query's own ``limit`` bounds the fan-out;
        further shaping (caps, excerpts) is deliberately unbuilt until
        dogfooding with the agent LLM shows what it needs.
        """
        bodies = await asyncio.gather(
            *(self._repository.fetch_body(node_id) for node_id in node_ids)
        )
        return dict(zip(node_ids, bodies, strict=True))

    async def find_path(
        self,
        source: NodeId | None,
        target: NodeId,
        *,
        edge_types: Iterable[str] | None = None,
        max_length: int = pathfinding.DEFAULT_MAX_LENGTH,
    ) -> Path | None:
        """Shortest meaningful path; ``source=None`` uses the session default."""
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
        default = self._session.default_start()
        if default is None:
            raise NoDefaultStart()
        return default
