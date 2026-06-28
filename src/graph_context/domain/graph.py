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
    * Self-loops are dropped (an object relating to itself is not a graph edge).
    * Removing a node removes its incident edges.

Edges are *open*: any relation label is admissible (the space-reflecting
model reads whatever ``objects`` relations exist), so there is no longer a
schema endpoint-rule check here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from enum import StrEnum

from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import INFRA_ROLES
from graph_context.errors import AmbiguousNodeName, NodeNotFound


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

    # -- name resolution ------------------------------------------------
    #
    # Anytype's source-of-truth ids are opaque CIDs; an LLM holds *names*.
    # Since the whole world is in memory here, name lookup is a cheap scan,
    # so the tool layer can accept a name anywhere an id is expected and
    # resolve it through here. ``node()`` above stays strictly id-only -- it
    # backs edge-endpoint invariants that must never name-match.

    def find_by_name(
        self,
        query: str,
        node_type: str | None = None,
        limit: int = 10,
    ) -> list[Node]:
        """Nodes whose name matches ``query`` (case-insensitive).

        Exact-name matches win outright; only if there are none do
        substring matches apply (capped at ``limit``). Bookkeeping roles
        (Prose/SessionContext) are excluded so a bare name never resolves to
        an infrastructure node. ``node_type`` optionally filters on the type
        display label. Results are sorted by name then id (deterministic).
        """
        q = query.strip().casefold()
        if not q:
            return []
        type_q = node_type.strip().casefold() if node_type else None

        def candidate(node: Node) -> bool:
            if node.role in INFRA_ROLES:
                return False
            return type_q is None or node.type.casefold() == type_q

        def ordering(node: Node) -> tuple[str, str]:
            return (node.name.casefold(), node.id)

        pool = [n for n in self._nodes.values() if candidate(n)]
        exact = sorted(
            (n for n in pool if n.name.casefold() == q), key=ordering
        )
        if exact:
            return exact
        substring = sorted(
            (n for n in pool if q in n.name.casefold()), key=ordering
        )
        return substring[:limit]

    def resolve(self, identifier: str) -> Node:
        """Resolve an id *or* a name to a single node.

        A real id wins immediately (CIDs are unique and can't collide with a
        name). Otherwise the identifier is treated as a name: a unique match
        resolves, no match raises :class:`NodeNotFound`, and multiple matches
        raise :class:`AmbiguousNodeName` listing the candidates.
        """
        node = self._nodes.get(identifier)
        if node is not None:
            return node
        matches = self.find_by_name(identifier)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NodeNotFound(identifier)
        raise AmbiguousNodeName(
            identifier,
            tuple((n.name, n.type, n.id) for n in matches),
        )

    # -- edges ----------------------------------------------------------

    def add_edge(self, edge: Edge) -> None:
        """Add a labelled edge, enforcing endpoint existence.

        Both endpoints must exist (raises :class:`NodeNotFound` otherwise).
        Self-loops (``source == target``) are silently skipped -- an untyped
        self-reference (e.g. an object's generic ``links`` pointing at itself)
        is not a meaningful graph edge.
        """
        self.node(edge.source)
        self.node(edge.target)
        if edge.source == edge.target:
            return
        self._out[edge.source].add(edge)
        self._in[edge.target].add(edge)

    def remove_edge(self, edge: Edge) -> None:
        self._out[edge.source].discard(edge)
        self._in[edge.target].discard(edge)

    def edges(
        self,
        node_id: NodeId,
        direction: Direction = Direction.BOTH,
        edge_types: Iterable[str] | None = None,
    ) -> Iterator[Edge]:
        """Yield edges incident to ``node_id``, optionally filtered by label."""
        allowed = frozenset(edge_types) if edge_types is not None else None
        if direction in (Direction.OUT, Direction.BOTH):
            yield from self._filtered(self._out[node_id], allowed)
        if direction in (Direction.IN, Direction.BOTH):
            yield from self._filtered(self._in[node_id], allowed)

    def neighbors(
        self,
        node_id: NodeId,
        direction: Direction = Direction.BOTH,
        edge_types: Iterable[str] | None = None,
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

    def degree(self, node_id: NodeId) -> int:
        """Incident-edge count (in + out). 0 for an isolated or unknown id.

        Out- and in-edge sets are disjoint (self-loops are dropped at
        ``add_edge``), so the sum is the true incident count. No existence
        check, consistent with ``edges()`` -- cheap enough to rank every node.
        """
        return len(self._out[node_id]) + len(self._in[node_id])

    @staticmethod
    def _filtered(
        edges: set[Edge], allowed: frozenset[str] | None
    ) -> Iterator[Edge]:
        for edge in edges:
            if allowed is None or edge.type in allowed:
                yield edge
