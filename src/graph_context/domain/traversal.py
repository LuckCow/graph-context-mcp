"""Bounded breadth-first traversal: the engine behind the ``explore`` tool.

Everything here is a pure function over a :class:`GraphIndex`; the
application layer is responsible for resolving defaults (focus-stack start
node) and the interface layer for shaping detail levels. This separation is
what makes the trickiest logic in the system exhaustively unit-testable.

Semantics worth knowing (and testing):
    * The start node is always included as a depth-0 hit and is exempt
      from node-type filters (you asked to start there).
    * Node filters *prune subtrees*: a filtered-out node is neither
      returned nor traversed through. This keeps "explore Characters only"
      from leaking paths through excluded node kinds, at the cost of not
      seeing past them -- raise ``depth``/widen filters when that matters.
    * ``as_of`` hides Event nodes with ``story_time`` strictly greater
      than the cutoff unless ``include_future=True`` (foreshadowing mode).
    * ``limit`` caps non-start hits; ``truncated=True`` signals the cap
      was hit so the caller (ultimately the LLM) knows to narrow or raise.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, Node, NodeId, TimelineValue
from graph_context.domain.schema import Role
from graph_context.errors import GraphContextError

MAX_DEPTH = 3
DEFAULT_DEPTH = 1
DEFAULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class ExploreQuery:
    """Parameter object for :func:`explore` (mirrors the MCP tool surface)."""

    start: NodeId
    depth: int = DEFAULT_DEPTH
    include_node_types: frozenset[str] | None = None
    exclude_node_types: frozenset[str] = frozenset()
    edge_types: frozenset[str] | None = None
    as_of: TimelineValue | None = None
    include_future: bool = False
    limit: int = DEFAULT_LIMIT
    exclude_roles: frozenset[Role] = frozenset()


@dataclass(frozen=True, slots=True)
class ExploreHit:
    """One node reached by the traversal, with provenance."""

    node: Node
    depth: int
    via: Edge | None  # None only for the start node


@dataclass(frozen=True, slots=True)
class ExploreResult:
    hits: tuple[ExploreHit, ...]
    truncated: bool


def explore(graph: GraphIndex, query: ExploreQuery) -> ExploreResult:
    """Run a bounded, filtered BFS from ``query.start``."""
    depth_cap = max(0, min(query.depth, MAX_DEPTH))
    start = graph.node(query.start)

    hits: list[ExploreHit] = [ExploreHit(node=start, depth=0, via=None)]
    visited: set[NodeId] = {start.id}
    frontier: deque[tuple[NodeId, int]] = deque([(start.id, 0)])
    truncated = False
    found = 0  # non-start hits, compared against query.limit

    while frontier and not truncated:
        node_id, depth = frontier.popleft()
        if depth == depth_cap:
            continue
        for edge, neighbor in graph.neighbors(node_id, edge_types=query.edge_types):
            if neighbor.id in visited:
                continue
            visited.add(neighbor.id)
            if not _admits(neighbor, query):
                continue  # pruned: not returned, not traversed through
            if found >= query.limit:
                truncated = True
                break
            hits.append(ExploreHit(node=neighbor, depth=depth + 1, via=edge))
            found += 1
            frontier.append((neighbor.id, depth + 1))

    return ExploreResult(hits=tuple(hits), truncated=truncated)


def _node_identifiers(node: Node) -> set[str]:
    """The strings a type filter may match a node by: display name, raw type
    key, and role name. Lets ``include_types=["Prose"]`` work whether the
    caller names the display type, the type key, or the role."""
    identifiers = {node.type, node.type_key}
    if node.role is not None:
        identifiers.add(node.role.value)
    return {i for i in identifiers if i}


def _admits(node: Node, query: ExploreQuery) -> bool:
    """Apply role, node-type, and story-time filters to a candidate hit."""
    identifiers = _node_identifiers(node)
    if node.role in query.exclude_roles:
        return False
    if identifiers & query.exclude_node_types:
        return False
    if query.include_node_types is not None and not (
        identifiers & query.include_node_types
    ):
        return False
    if (  # noqa: SIM103 -- early-return chain reads clearer than one boolean
        query.as_of is not None
        and not query.include_future
        and node.role is Role.EVENT
        and node.story_time is not None
        and _after(node.story_time, query.as_of)
    ):
        return False
    return True


def _after(story_time: TimelineValue, as_of: TimelineValue) -> bool:
    """``story_time > as_of`` with an ACTIONABLE mixed-type failure.

    A space uses one timeline representation (ADR 015: numbers for
    fiction, ISO dates for a date-axis profile); the guard turns the
    TypeError an LLM would trip with a wrong-typed ``as_of`` into a
    self-correcting message.
    """
    try:
        return story_time > as_of  # type: ignore[operator]
    except TypeError:
        raise GraphContextError(
            f"as_of {as_of!r} is not comparable with this space's timeline "
            f"values (e.g. {story_time!r}); pass as_of in the same form as "
            "story_time values shown by get_node/explore"
        ) from None
