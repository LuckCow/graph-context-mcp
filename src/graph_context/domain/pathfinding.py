"""Bounded shortest-path search: the engine behind the ``find_path`` tool.

Paths are found over the *undirected* view of the graph (an edge can be
walked either way) because narrative connections do not care about edge
direction -- "how are Mira and the Siege of Brakk connected?" should find
``Mira -participated_in-> Siege`` regardless of arrow direction. The
returned edges preserve their stored direction so callers can render
"Mira participated_in Siege" faithfully.

``edge_types`` restricts which edges may be walked (the proposal's
"meaningful paths"); ``max_length`` bounds the search so a mature graph
cannot make this expensive or return narratively useless 9-hop chains.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, Node, NodeId

DEFAULT_MAX_LENGTH = 4


@dataclass(frozen=True, slots=True)
class Path:
    """A connected sequence: ``nodes[i] -edges[i]- nodes[i+1]``."""

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def __len__(self) -> int:
        return len(self.edges)


def find_path(
    graph: GraphIndex,
    source: NodeId,
    target: NodeId,
    *,
    edge_types: Iterable[str] | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> Path | None:
    """Return a shortest path from ``source`` to ``target``, or ``None``.

    Raises :class:`graph_context.errors.NodeNotFound` if either endpoint
    is missing -- a missing node is a caller error, while "no path" is an
    ordinary, meaningful answer.
    """
    graph.node(source)
    graph.node(target)
    if source == target:
        return Path(nodes=(graph.node(source),), edges=())

    allowed = frozenset(edge_types) if edge_types is not None else None
    came_from: dict[NodeId, tuple[NodeId, Edge]] = {}
    visited: set[NodeId] = {source}
    frontier: deque[tuple[NodeId, int]] = deque([(source, 0)])

    while frontier:
        node_id, length = frontier.popleft()
        if length == max_length:
            continue
        for edge, neighbor in graph.neighbors(node_id, edge_types=allowed):
            if neighbor.id in visited:
                continue
            visited.add(neighbor.id)
            came_from[neighbor.id] = (node_id, edge)
            if neighbor.id == target:
                return _reconstruct(graph, source, target, came_from)
            frontier.append((neighbor.id, length + 1))

    return None


def _reconstruct(
    graph: GraphIndex,
    source: NodeId,
    target: NodeId,
    came_from: dict[NodeId, tuple[NodeId, Edge]],
) -> Path:
    node_ids: list[NodeId] = [target]
    edges: list[Edge] = []
    cursor = target
    while cursor != source:
        cursor, edge = came_from[cursor]
        node_ids.append(cursor)
        edges.append(edge)
    node_ids.reverse()
    edges.reverse()
    return Path(
        nodes=tuple(graph.node(node_id) for node_id in node_ids),
        edges=tuple(edges),
    )
