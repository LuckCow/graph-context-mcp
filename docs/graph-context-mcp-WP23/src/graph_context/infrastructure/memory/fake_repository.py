"""In-memory reference implementation of :class:`GraphRepository`.

Two jobs:

1. **Tests and offline development.** Application services are exercised
   against this fake, so the whole use-case layer is testable without a
   running Anytype instance.
2. **Executable specification.** ``AnytypeGraphRepository`` must match this
   behaviour exactly (see ``tests/contract``) -- in particular the
   composite-create rollback contract -- with the only difference being
   write-through persistence to the Anytype API and id assignment by
   Anytype.

Ids here are sequential (``n0001``...) purely for readable test output.
The methods are ``async`` to satisfy the port, but never actually await.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import count

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, LinkSpec, Node, NodeDraft, NodeId


class InMemoryGraphRepository:
    """``GraphRepository`` whose only store is its own :class:`GraphIndex`."""

    def __init__(self) -> None:
        self._graph = GraphIndex()
        self._ids = count(1)
        self._bodies: dict[NodeId, str] = {}

    @property
    def graph(self) -> GraphIndex:
        return self._graph

    async def create_node(
        self, draft: NodeDraft, links: Sequence[LinkSpec] = ()
    ) -> Node:
        node = Node(
            id=f"n{next(self._ids):04d}",
            type=draft.type,
            name=draft.name,
            summary=draft.summary,
            description=draft.description,
            story_time=draft.story_time,
            fields=dict(draft.fields),
        )
        self._graph.upsert_node(node)
        if draft.body:
            self._bodies[node.id] = draft.body
        try:
            for link in links:
                self._graph.add_edge(link.to_edge(anchor=node.id))
        except Exception:
            # Composite-create contract: never leave a half-applied write.
            self._graph.remove_node(node.id)
            raise
        return node

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        summary_stale: bool | None = None,
        description: str | None = None,
        story_time: float | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Node:
        changes = {
            key: value
            for key, value in {
                "name": name,
                "summary": summary,
                "summary_stale": summary_stale,
                "description": description,
                "story_time": story_time,
                "fields": dict(fields) if fields is not None else None,
            }.items()
            if value is not None
        }
        updated = replace(self._graph.node(node_id), **changes)
        self._graph.upsert_node(updated)
        return updated

    async def add_link(self, anchor: NodeId, link: LinkSpec) -> Edge:
        edge = link.to_edge(anchor=anchor)
        self._graph.add_edge(edge)
        return edge

    async def remove_link(self, edge: Edge) -> None:
        self._graph.remove_edge(edge)

    async def fetch_body(self, node_id: NodeId) -> str:
        self._graph.node(node_id)  # NodeNotFound on bad id
        return self._bodies.get(node_id, "")

    async def hydrate(self) -> None:
        """No backing store: the index is already authoritative here."""

    async def resync(self) -> frozenset[NodeId]:
        """No out-of-band editors can exist for an in-memory store."""
        return frozenset()
