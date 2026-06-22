"""Use-case: in-depth single-node retrieval (the ``get_node`` tool).

Returns a :class:`NodeView`: the node plus its incident edges grouped by
edge type, each with the neighbor attached so the presenter can render
"participated_in -> Siege of Brakk" without further lookups.

WP3 ``include_prose`` (resolved): the "how was this place described last
time?" consistency lookup. When ``include_prose`` > 0, the reader returns
up to that many Prose nodes that ``references`` this node, most-recent
first (by Prose ``fields["generated_at"]``), each with a body excerpt
fetched on demand via ``repository.fetch_body`` and capped at
``excerpt_chars``. The reverse-reference lookup is one index call:
``graph.neighbors(node_id, Direction.IN, edge_types=[EdgeType.REFERENCES])``.

The excerpt budget (``excerpt_chars``) is a *presentation* concern and is
injected by the tool layer (default keeps this use-case self-contained);
the application layer never imports the interface layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from graph_context.domain.graph import Direction
from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import EdgeType
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository

DEFAULT_EXCERPT_CHARS = 300  # mirror of presenters.PROSE_EXCERPT_CHARS default


@dataclass(frozen=True, slots=True)
class NodeView:
    node: Node
    # edge type -> ((edge, neighbor), ...); both directions, presenter
    # renders the arrow by comparing edge.source with node.id.
    edges: dict[EdgeType, tuple[tuple[Edge, Node], ...]]
    # WP3: (prose node, body excerpt) pairs, most-recent first; empty
    # unless include_prose was requested.
    prose: tuple[tuple[Node, str], ...] = field(default=())


class NodeReader:
    """Session-aware deep read of one node."""

    def __init__(self, repository: GraphRepository, session: SessionState) -> None:
        self._repository = repository
        self._session = session

    async def get_node(
        self,
        node_id: NodeId,
        *,
        edge_type_filter: Iterable[EdgeType] | None = None,
        include_prose: int = 0,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> NodeView:
        graph = self._repository.graph
        node = graph.node(node_id)
        grouped: dict[EdgeType, list[tuple[Edge, Node]]] = {}
        for edge, neighbor in graph.neighbors(
            node_id, Direction.BOTH, edge_types=edge_type_filter
        ):
            grouped.setdefault(edge.type, []).append((edge, neighbor))
        prose: tuple[tuple[Node, str], ...] = ()
        if include_prose > 0:
            prose = await self._recent_prose(node_id, include_prose, excerpt_chars)
        self._session.touch(node_id)
        return NodeView(
            node=node,
            edges={k: tuple(v) for k, v in sorted(grouped.items(), key=lambda i: i[0].value)},
            prose=prose,
        )

    async def _recent_prose(
        self, node_id: NodeId, limit: int, excerpt_chars: int
    ) -> tuple[tuple[Node, str], ...]:
        graph = self._repository.graph
        # Incoming `references` edges originate on Prose nodes (Prose -> here).
        prose_nodes = [
            neighbor
            for _, neighbor in graph.neighbors(
                node_id, Direction.IN, edge_types=[EdgeType.REFERENCES]
            )
        ]
        prose_nodes.sort(
            key=lambda n: n.fields.get("generated_at", ""), reverse=True
        )
        out: list[tuple[Node, str]] = []
        for prose_node in prose_nodes[:limit]:
            body = await self._repository.fetch_body(prose_node.id)
            excerpt = body[:excerpt_chars]
            if len(body) > excerpt_chars:
                excerpt += "…"
            out.append((prose_node, excerpt))
        return tuple(out)
