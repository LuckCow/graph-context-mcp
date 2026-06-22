"""Use-case: in-depth single-node retrieval (the ``get_node`` tool).

Returns a :class:`NodeView`: the node plus its incident edges grouped by
edge type, each with the neighbor attached so the presenter can render
"participated_in -> Siege of Brakk" without further lookups.

WP3 wiring point: ``include_prose`` (int, most-recent-N Prose nodes that
``references`` this node, with excerpts via ``repository.fetch_body``).
The reverse-reference lookup is one index call:
``graph.neighbors(node_id, Direction.IN, edge_types=[EdgeType.REFERENCES])``.
TODO(junior): add the parameter + excerpt fetching here and surface it in
the tool layer; ordering = most recent first (Prose ``fields["generated_at"]``),
excerpt cap = presenters.PROSE_EXCERPT_CHARS. See WORK_PACKAGES WP3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from graph_context.domain.graph import Direction
from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import EdgeType
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository


@dataclass(frozen=True, slots=True)
class NodeView:
    node: Node
    # edge type -> ((edge, neighbor), ...); both directions, presenter
    # renders the arrow by comparing edge.source with node.id.
    edges: dict[EdgeType, tuple[tuple[Edge, Node], ...]]


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
    ) -> NodeView:
        graph = self._repository.graph
        node = graph.node(node_id)
        grouped: dict[EdgeType, list[tuple[Edge, Node]]] = {}
        for edge, neighbor in graph.neighbors(
            node_id, Direction.BOTH, edge_types=edge_type_filter
        ):
            grouped.setdefault(edge.type, []).append((edge, neighbor))
        self._session.touch(node_id)
        return NodeView(
            node=node,
            edges={k: tuple(v) for k, v in sorted(grouped.items(), key=lambda i: i[0].value)},
        )
