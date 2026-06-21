"""GraphIndex: the in-memory adjacency projection of the story world.

Architectural role
------------------
Anytype is the source of truth and the human editing surface; its API can
only text-search names/snippets, so all traversal happens here. The index
is therefore a *derived, rebuildable projection*:

    * Repository adapters write through to it on every mediated write.
    * It can be re-hydrated wholesale from storage (project open / resync),
      including incrementally via last-modified timestamps when humans edit
      the world out-of-band in the Anytype UI.
    * Losing it loses nothing.

Story worlds are small (thousands of nodes), so plain dict/set adjacency
is simpler and faster than any external graph engine at this scale.

Invariants enforced here (the single choke point for edges):
    * Both endpoints of an edge must exist.
    * Endpoint types must satisfy the schema's edge rules.
    * Removing a node removes its incident edges.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from enum import StrEnum

from graph_context.domain import schema
from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import EdgeType
from graph_context.errors import NodeNotFound


class Direction(StrEnum):
    """Which incident edges of a node to consider."""

    OUT = "out"
    IN = "in"
    BOTH = "both"


class GraphIndex:
    """Mutable adjacency index over immutable :class:`Node` values."""

    def __init__(self) -> None:
        self._nodes: dict[NodeId, Node] = {}
        self._out: dict[NodeId, set[Edge]] = defaultdict(set)
        self._in: dict[NodeId, set[Edge]] = defaultdict(set)

    # -- nodes ----------------------------------------------------------

    def upsert_node(self, node: Node) -> None:
        """Insert ``node`` or replace the stored value with the same id."""
        self._nodes[node.id] = node

    def remove_node(self, node_id: NodeId) -> None:
        """Remove a node and every edge incident to it."""
        self.node(node_id)  # existence check
        for edge in list(self._out[node_id]):
            self.remove_edge(edge)
        for edge in list(self._in[node_id]):
            self.remove_edge(edge)
        del self._nodes[node_id]

    def node(self, node_id: NodeId) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise NodeNotFound(node_id) from None

    def has_node(self, node_id: NodeId) -> bool:
        return node_id in self._nodes

    def nodes(self) -> Iterator[Node]:
        return iter(self._nodes.values())

    # -- edges ----------------------------------------------------------

    def add_edge(self, edge: Edge) -> None:
        """Add a typed edge, enforcing existence and schema endpoint rules."""
        source = self.node(edge.source)
        target = self.node(edge.target)
        schema.validate_edge(source.type, edge.type, target.type)
        self._out[edge.source].add(edge)
        self._in[edge.target].add(edge)

    def remove_edge(self, edge: Edge) -> None:
        self._out[edge.source].discard(edge)
        self._in[edge.target].discard(edge)

    def edges(
        self,
        node_id: NodeId,
        direction: Direction = Direction.BOTH,
        edge_types: Iterable[EdgeType] | None = None,
    ) -> Iterator[Edge]:
        """Yield edges incident to ``node_id``, optionally filtered by type."""
        allowed = frozenset(edge_types) if edge_types is not None else None
        if direction in (Direction.OUT, Direction.BOTH):
            yield from self._filtered(self._out[node_id], allowed)
        if direction in (Direction.IN, Direction.BOTH):
            yield from self._filtered(self._in[node_id], allowed)

    def neighbors(
        self,
        node_id: NodeId,
        direction: Direction = Direction.BOTH,
        edge_types: Iterable[EdgeType] | None = None,
    ) -> Iterator[tuple[Edge, Node]]:
        """Yield ``(edge, neighbor)`` pairs around ``node_id``."""
        for edge in self.edges(node_id, direction, edge_types):
            other_id = edge.target if edge.source == node_id else edge.source
            yield edge, self.node(other_id)

    # -- stats ----------------------------------------------------------

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return sum(len(edges) for edges in self._out.values())

    @staticmethod
    def _filtered(
        edges: set[Edge], allowed: frozenset[EdgeType] | None
    ) -> Iterator[Edge]:
        for edge in edges:
            if allowed is None or edge.type in allowed:
                yield edge
