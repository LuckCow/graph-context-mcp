"""Attribute queries over the whole graph: the engine behind ``query``.

Where :mod:`graph_context.domain.traversal` walks outward from a start
node, this module scans a candidate set (the whole corpus, or one node's
neighborhood when anchored via ``linked_to``) and applies field predicates,
multi-key ordering, and a limit -- the Anytype-Set experience, executed on
the derived :class:`GraphIndex` (ADR 018 extends ADR 002: the index is the
only query engine).

Semantic contracts the LLM builds habits on (each pinned by tests):
    * Values are strings (ADR 012 reflection). Two values compare
      numerically when BOTH parse as floats, else casefolded
      lexicographically -- ISO dates therefore order chronologically.
    * Absent keys: ``eq``/``lt``/``lte``/``gt``/``gte``/``contains``/
      ``exists`` never match a node lacking the field; ``neq`` DOES match
      absence ("not known to be value"). This makes ``done neq true`` the
      open-todos idiom, because an unticked Anytype checkbox is stored as
      absence, not ``"false"`` (mapping quirk A-series).
    * ``missing`` matches only absence.
    * Nodes missing a sort key order last regardless of direction; final
      tie-break is ``(name, id)`` like ``find_by_name``.
    * A predicate/sort field that appears on no candidate at all (and is
      not a built-in) is an error naming the fields that DO exist -- a
      zero-occurrence key is a typo, while per-node absence is ordinary
      sparseness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import cmp_to_key

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Node, NodeId
from graph_context.domain.schema import Role
from graph_context.domain.traversal import node_identifiers
from graph_context.errors import GraphContextError

MAX_LIMIT = 100
DEFAULT_LIMIT = 25

#: Node attributes queryable by every node, before type-specific ``fields``.
BUILTIN_FIELDS: tuple[str, ...] = (
    "name",
    "type",
    "summary",
    "story_time",
    "modified_at",
    "summary_stale",
)

#: Cap on how many observed field names an unknown-field error lists.
_ERROR_FIELD_CAP = 30


class Op(StrEnum):
    """Predicate operators. ``exists``/``missing`` take no value."""

    EQ = "eq"
    NEQ = "neq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    CONTAINS = "contains"
    EXISTS = "exists"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Predicate:
    """One ``field op value`` condition; predicates AND together."""

    field: str
    op: Op
    value: str = ""


@dataclass(frozen=True, slots=True)
class SortKey:
    field: str
    descending: bool = False


@dataclass(frozen=True, slots=True)
class NodeQuery:
    """Parameter object for :func:`run_query` (mirrors the MCP tool surface).

    ``linked_to`` anchors the candidate set to one node's direct neighbors
    (both directions), optionally constrained by ``edge_types`` -- e.g. a
    character's timeline is ``node_type="Event", linked_to=<character>,
    order_by=(SortKey("story_time"),)``. Deeper reach is ``explore``'s job.
    """

    node_type: str | None = None
    linked_to: NodeId | None = None
    edge_types: frozenset[str] | None = None
    predicates: tuple[Predicate, ...] = ()
    order_by: tuple[SortKey, ...] = ()
    limit: int = DEFAULT_LIMIT
    exclude_roles: frozenset[Role] = frozenset()


@dataclass(frozen=True, slots=True)
class QueryResult:
    """``matched`` counts before the limit cut: "10 of 37" is the signal to
    tighten predicates or raise the limit."""

    hits: tuple[Node, ...]
    matched: int
    truncated: bool


def run_query(graph: GraphIndex, query: NodeQuery) -> QueryResult:
    """Filter, order, and cap nodes per ``query``."""
    limit = max(1, min(query.limit, MAX_LIMIT))
    candidates = _candidates(graph, query)
    if not candidates:
        return QueryResult(hits=(), matched=0, truncated=False)
    _validate_fields(candidates, query)
    matches = [
        node
        for node in candidates
        if all(_matches(node, predicate) for predicate in query.predicates)
    ]
    ordered = sorted(matches, key=cmp_to_key(_comparator(query.order_by)))
    hits = tuple(ordered[:limit])
    return QueryResult(
        hits=hits, matched=len(ordered), truncated=len(ordered) > len(hits)
    )


def normalize_value(value: object) -> str:
    """A JSON-typed comparison value -> the index's string representation.

    ``str(True)`` is ``"True"`` but a ticked checkbox stores ``"true"``,
    and reflection strips integral floats' ``.0`` -- predicate values
    from ANY source (MCP tool arguments, compiled Set views) must be
    normalized the same way, so the rule lives here, once.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def field_value(node: Node, field_name: str) -> str | None:
    """A node's value for ``field_name``, or ``None`` when absent.

    Built-ins first, then ``node.fields`` by exact key, then by
    case-insensitive key (Anytype property keys are snake_case; the LLM
    sees display-ish names and should not have to guess casing).
    """
    key = field_name.strip()
    low = key.casefold()
    if low == "name":
        return node.name
    if low == "type":
        return node.type
    if low == "summary":
        return node.summary
    if low == "story_time":
        return None if node.story_time is None else str(node.story_time)
    if low == "modified_at":
        return node.modified_at or None
    if low == "summary_stale":
        return "true" if node.summary_stale else "false"
    if key in node.fields:
        return node.fields[key]
    for field_key, value in node.fields.items():
        if field_key.casefold() == low:
            return value
    return None


# -- candidate collection ------------------------------------------------


def _candidates(graph: GraphIndex, query: NodeQuery) -> list[Node]:
    if query.linked_to is not None:
        graph.node(query.linked_to)  # NodeNotFound on a bad anchor
        pool: list[Node] = []
        seen: set[NodeId] = set()
        for _, neighbor in graph.neighbors(
            query.linked_to, edge_types=query.edge_types
        ):
            if neighbor.id not in seen:  # parallel edges: one candidacy
                seen.add(neighbor.id)
                pool.append(neighbor)
    else:
        pool = list(graph.nodes())
    return [node for node in pool if _admits(node, query)]


def _admits(node: Node, query: NodeQuery) -> bool:
    if node.role in query.exclude_roles:
        return False
    if query.node_type is None:
        return True
    wanted = query.node_type.casefold()
    return any(i.casefold() == wanted for i in node_identifiers(node))


# -- field validation ------------------------------------------------------


def _validate_fields(candidates: list[Node], query: NodeQuery) -> None:
    """Reject fields that no candidate carries: catches typos before they
    silently match nothing (or, worse, ``missing``/``neq`` matching all)."""
    referenced = {p.field for p in query.predicates}
    referenced.update(k.field for k in query.order_by)
    builtins = {f.casefold() for f in BUILTIN_FIELDS}
    observed = {
        key.casefold(): key for node in candidates for key in node.fields
    }
    for name in sorted(referenced):
        low = name.strip().casefold()
        if low in builtins or low in observed:
            continue
        available = sorted(observed.values())[:_ERROR_FIELD_CAP]
        listing = ", ".join((*BUILTIN_FIELDS, *available))
        raise GraphContextError(
            f"no queried node has a field named {name!r}; queryable fields "
            f"here: {listing}"
        )


# -- predicates ------------------------------------------------------------


def _matches(node: Node, predicate: Predicate) -> bool:
    value = field_value(node, predicate.field)
    if predicate.op is Op.MISSING:
        return value is None
    if predicate.op is Op.EXISTS:
        return value is not None
    if predicate.op is Op.NEQ:
        # Absence matches: "not known to be value". The open-todos idiom.
        return value is None or not _equal(value, predicate.value)
    if value is None:
        return False
    if predicate.op is Op.EQ:
        return _equal(value, predicate.value)
    if predicate.op is Op.CONTAINS:
        return predicate.value.casefold() in value.casefold()
    ordering = _compare(value, predicate.value)
    if predicate.op is Op.LT:
        return ordering < 0
    if predicate.op is Op.LTE:
        return ordering <= 0
    if predicate.op is Op.GT:
        return ordering > 0
    return ordering >= 0  # Op.GTE -- the enum is exhausted


# -- value comparison (the ONE place string coercion lives) ----------------


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _equal(left: str, right: str) -> bool:
    lf, rf = _as_float(left), _as_float(right)
    if lf is not None and rf is not None:
        return lf == rf  # "5" equals "5.0"
    return left.casefold() == right.casefold()


def _compare(left: str, right: str) -> int:
    """Three-way compare: numeric when both sides parse, else casefolded
    lexicographic (ISO dates thereby order chronologically)."""
    lf, rf = _as_float(left), _as_float(right)
    if lf is not None and rf is not None:
        return (lf > rf) - (lf < rf)
    lc, rc = left.casefold(), right.casefold()
    return (lc > rc) - (lc < rc)


# -- ordering ---------------------------------------------------------------


def _comparator(order_by: tuple[SortKey, ...]) -> Callable[[Node, Node], int]:
    def compare(a: Node, b: Node) -> int:
        for key in order_by:
            va = field_value(a, key.field)
            vb = field_value(b, key.field)
            if va is None and vb is None:
                continue
            if va is None:  # missing sorts last regardless of direction
                return 1
            if vb is None:
                return -1
            ordering = _compare(va, vb)
            if ordering:
                return -ordering if key.descending else ordering
        tie_a = (a.name.casefold(), a.id)
        tie_b = (b.name.casefold(), b.id)
        return (tie_a > tie_b) - (tie_a < tie_b)

    return compare
