"""Fixed v1 schema: the closed type vocabulary and its structural rules.

Design notes (see proposal, "Typed everything"):
    * The vocabulary is deliberately a *closed set* for v1. Type
      extensibility (``propose_type``) is a Phase 4 concern; until then,
      every node and edge the system touches is one of the members below.
    * Edge rules are endpoint constraints: which node types may appear at
      the source / target of each edge type. ``None`` means "any type".
      These rules are enforced at the single choke point where edges enter
      the graph (:meth:`graph_context.domain.graph.GraphIndex.add_edge`),
      so no other layer needs to re-check them.

This module is pure data + validation. It must never import from
application, ports, or infrastructure.
"""

from __future__ import annotations

from enum import StrEnum

from graph_context.errors import SchemaViolation


class NodeType(StrEnum):
    """Every kind of node a story-world graph may contain (v1, fixed)."""

    CHARACTER = "Character"
    LOCATION = "Location"
    EVENT = "Event"
    TECHNOLOGY = "Technology"
    FACTION = "Faction"
    ITEM = "Item"
    PROSE = "Prose"
    SESSION_CONTEXT = "SessionContext"


class EdgeType(StrEnum):
    """Every kind of typed link between nodes (v1, fixed)."""

    KNOWS = "knows"
    LOCATED_AT = "located_at"
    MEMBER_OF = "member_of"
    PARTICIPATED_IN = "participated_in"
    CAUSED = "caused"
    POSSESSES = "possesses"
    PARENT_OF = "parent_of"
    CHILD_OF = "child_of"
    REFERENCES = "references"
    PRECEDES = "precedes"


_ANY = None  # readability alias for "no endpoint constraint"

# edge type -> (allowed source types, allowed target types); None = any.
_EDGE_RULES: dict[
    EdgeType, tuple[frozenset[NodeType] | None, frozenset[NodeType] | None]
] = {
    EdgeType.KNOWS: (frozenset({NodeType.CHARACTER}), _ANY),
    EdgeType.LOCATED_AT: (_ANY, frozenset({NodeType.LOCATION})),
    EdgeType.MEMBER_OF: (frozenset({NodeType.CHARACTER}), frozenset({NodeType.FACTION})),
    EdgeType.PARTICIPATED_IN: (
        frozenset({NodeType.CHARACTER, NodeType.FACTION, NodeType.ITEM}),
        frozenset({NodeType.EVENT}),
    ),
    EdgeType.CAUSED: (_ANY, frozenset({NodeType.EVENT})),
    EdgeType.POSSESSES: (
        frozenset({NodeType.CHARACTER, NodeType.FACTION}),
        frozenset({NodeType.ITEM, NodeType.TECHNOLOGY}),
    ),
    EdgeType.PARENT_OF: (_ANY, _ANY),
    EdgeType.CHILD_OF: (_ANY, _ANY),
    EdgeType.REFERENCES: (frozenset({NodeType.PROSE}), _ANY),
    EdgeType.PRECEDES: (frozenset({NodeType.EVENT}), frozenset({NodeType.EVENT})),
}


def validate_edge(source: NodeType, edge_type: EdgeType, target: NodeType) -> None:
    """Raise :class:`SchemaViolation` if the endpoints are illegal for the edge type."""
    allowed_sources, allowed_targets = _EDGE_RULES[edge_type]
    if allowed_sources is not None and source not in allowed_sources:
        raise SchemaViolation(
            f"edge '{edge_type.value}' cannot start from a {source.value} "
            f"(allowed: {sorted(t.value for t in allowed_sources)})"
        )
    if allowed_targets is not None and target not in allowed_targets:
        raise SchemaViolation(
            f"edge '{edge_type.value}' cannot point to a {target.value} "
            f"(allowed: {sorted(t.value for t in allowed_targets)})"
        )


def validate_new_node(
    node_type: NodeType,
    name: str,
    summary: str,
    story_time: float | None,
) -> None:
    """Enforce creation invariants from the proposal.

    * ``name`` and ``summary`` are required on every node ("forces the LLM
      to commit a one-liner at write time").
    * Events additionally require ``story_time`` (their position on the
      story timeline), because ``as_of`` filtering is meaningless without it.
    """
    if not name.strip():
        raise SchemaViolation("node 'name' must be a non-empty string")
    if not summary.strip():
        raise SchemaViolation("node 'summary' is required at creation time")
    if node_type is NodeType.EVENT and story_time is None:
        raise SchemaViolation("Event nodes require 'story_time' (timeline position)")
