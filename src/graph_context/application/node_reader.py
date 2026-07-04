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
``graph.neighbors(node_id, Direction.IN, edge_types=["references"])``.

The excerpt budget (``excerpt_chars``) is a *presentation* concern and is
injected by the tool layer (default keeps this use-case self-contained);
the application layer never imports the interface layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from graph_context.domain.graph import Direction
from graph_context.domain.models import Edge, Node, NodeId
from graph_context.domain.schema import INFRA_ROLES, Role
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository

DEFAULT_EXCERPT_CHARS = 300  # mirror of presenters.PROSE_EXCERPT_CHARS default

REFERENCES_LABEL = "references"  # Prose -> source edge label (cleaned)
INTENT_LABEL = "intent"  # intent node -> touched node edge label (WP7)


@dataclass(frozen=True, slots=True)
class NodeView:
    node: Node
    # edge label -> ((edge, neighbor), ...); both directions, presenter
    # renders the arrow by comparing edge.source with node.id.
    edges: dict[str, tuple[tuple[Edge, Node], ...]]
    # The node's long-form description, fetched on demand from the body
    # (ADR 010) -- get_node is "working with the node directly", so the
    # full text always rides along.
    body: str = ""
    # WP3: (prose node, body excerpt) pairs, most-recent first; empty
    # unless include_prose was requested.
    prose: tuple[tuple[Node, str], ...] = field(default=())
    # Total Prose passages referencing this node -- always populated
    # (index-only, no body fetches) so "does prose exist?" is a signal the
    # presenter can surface, never an inference.
    prose_count: int = 0
    # WP7: (intent node, body excerpt) pairs, most-recent first; empty
    # unless include_provenance was requested. Mirrors prose exactly.
    provenance: tuple[tuple[Node, str], ...] = field(default=())
    provenance_count: int = 0


class NodeReader:
    """Session-aware deep read of one node."""

    def __init__(self, repository: GraphRepository, session: SessionState) -> None:
        self._repository = repository
        self._session = session

    async def get_node(
        self,
        node_id: NodeId,
        *,
        edge_type_filter: Iterable[str] | None = None,
        include_prose: int = 0,
        include_provenance: int = 0,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> NodeView:
        graph = self._repository.graph
        node = graph.node(node_id)
        grouped: dict[str, list[tuple[Edge, Node]]] = {}
        for edge, neighbor in graph.neighbors(
            node_id, Direction.BOTH, edge_types=edge_type_filter
        ):
            # WP7: infra-role neighbors (Prose, SessionContext, Intent) are
            # bookkeeping -- their edges never clutter the edge groups. The
            # prose/provenance counts below are the deliberate signal.
            if neighbor.role in INFRA_ROLES:
                continue
            grouped.setdefault(edge.type, []).append((edge, neighbor))
        body = await self._repository.fetch_body(node_id)
        prose_nodes = self._referencing_prose(node_id)
        prose: tuple[tuple[Node, str], ...] = ()
        if include_prose > 0:
            prose = await self._excerpts(prose_nodes[:include_prose], excerpt_chars)
        intent_nodes = self._referencing_intents(node_id)
        provenance: tuple[tuple[Node, str], ...] = ()
        if include_provenance > 0:
            provenance = await self._excerpts(
                intent_nodes[:include_provenance], excerpt_chars
            )
        self._session.touch(node_id)
        return NodeView(
            node=node,
            edges={k: tuple(v) for k, v in sorted(grouped.items(), key=lambda i: i[0])},
            body=body,
            prose=prose,
            prose_count=len(prose_nodes),
            provenance=provenance,
            provenance_count=len(intent_nodes),
        )

    def _referencing_prose(self, node_id: NodeId) -> list[Node]:
        """Prose nodes referencing this node, most-recent first. Index-only.

        Incoming `references` edges originate on Prose nodes (Prose -> here);
        the role filter keeps a human-created `references` relation between
        story nodes from posing as prose.
        """
        prose_nodes = [
            neighbor
            for _, neighbor in self._repository.graph.neighbors(
                node_id, Direction.IN, edge_types=[REFERENCES_LABEL]
            )
            if neighbor.role is Role.PROSE
        ]
        prose_nodes.sort(
            key=lambda n: n.fields.get("generated_at", ""), reverse=True
        )
        return prose_nodes

    def _referencing_intents(self, node_id: NodeId) -> list[Node]:
        """Intent nodes that touched this node, most-recent first (WP7)."""
        intents = [
            neighbor
            for _, neighbor in self._repository.graph.neighbors(
                node_id, Direction.IN, edge_types=[INTENT_LABEL]
            )
            if neighbor.role is Role.INTENT
        ]
        intents.sort(key=lambda n: n.fields.get("generated_at", ""), reverse=True)
        return intents

    async def _excerpts(
        self, prose_nodes: list[Node], excerpt_chars: int
    ) -> tuple[tuple[Node, str], ...]:
        out: list[tuple[Node, str]] = []
        for prose_node in prose_nodes:
            body = await self._repository.fetch_body(prose_node.id)
            excerpt = body[:excerpt_chars]
            if len(body) > excerpt_chars:
                excerpt += "…"
            out.append((prose_node, excerpt))
        return tuple(out)
