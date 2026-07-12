"""Argument parsing for the tool surface: errors are written FOR the LLM.

Every ``_parse_*`` helper normalizes one tool parameter and, on a bad
value, raises a :class:`GraphContextError` that echoes the allowed
values -- the model reads the error and self-corrects. Resolution of
id-or-name inputs (``_resolve``) lives here too: it is the same
boundary, so application and domain code only ever see canonical ids.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graph_context.domain.models import LinkSpec, NodeId
from graph_context.domain.query import Op, Predicate, SortKey, normalize_value
from graph_context.domain.schema import Role
from graph_context.domain.traversal import node_identifiers
from graph_context.errors import GraphContextError, NodeNotFound, UnknownNodeType
from graph_context.interface import presenters
from graph_context.interface.presenters import Detail
from graph_context.interface.services import Services


async def _resolve(services: Services, identifier: str) -> NodeId:
    """Translate a user-supplied id-or-name into a real node id.

    Resolution is a tool-layer concern (the same boundary that does all
    ``_parse_*`` normalization), so the application and domain layers keep
    receiving canonical ids. Raises NodeNotFound/AmbiguousNodeName, both
    actionable, when the string does not resolve to exactly one node.

    ADR 016: on a miss, the Ranker (when wired) appends "closest by
    meaning" candidates WITH evidence to the error -- a suggestion
    surface, never silent resolution: exact resolves, fuzzy suggests,
    and mutation targets are never guessed (ADR 014 non-feature).
    """
    try:
        return services.repository.graph.resolve(identifier).id
    except NodeNotFound:
        if services.ranker is None:
            raise
        hits = await services.ranker.rank(identifier, limit=3)
        if not hits:
            raise
        raise NodeNotFound(
            identifier, suggestions=presenters.render_ranked_hits(hits)
        ) from None


def _parse_node_type(value: str) -> str:
    """Normalize a requested node type. The vocabulary is OPEN: validation
    (does this type exist in the space?) is the repository's job, which
    raises an actionable ``UnknownNodeType`` listing the known types."""
    normalized = value.strip()
    if not normalized:
        raise GraphContextError("node 'type' must be a non-empty string")
    return normalized


def _parse_edge_type(value: str) -> str:
    """Normalize a relation label. OPEN vocabulary: an unknown label is
    surfaced for approval by the repository, not rejected here."""
    normalized = value.strip()
    if not normalized:
        raise GraphContextError("each link needs a non-empty 'edge_type' label")
    return normalized


def _parse_detail(value: str) -> Detail:
    try:
        return Detail(value)
    except ValueError:
        raise GraphContextError(
            f"unknown detail level {value!r}; allowed: names, summaries, full"
        ) from None


async def _parse_links(
    raw: Sequence[dict[str, Any]] | None, services: Services
) -> list[LinkSpec]:
    links: list[LinkSpec] = []
    for item in raw or []:
        if "edge_type" not in item or "other" not in item:
            raise GraphContextError(
                "each link needs 'edge_type' and 'other' (target node id or "
                "name); optional 'outgoing' (default true; false means the "
                "edge points FROM 'other' TO this node)"
            )
        links.append(
            LinkSpec(
                edge_type=_parse_edge_type(str(item["edge_type"])),
                other=await _resolve(services, str(item["other"])),
                outgoing=bool(item.get("outgoing", True)),
            )
        )
    return links


_OPS_LISTING = ", ".join(op.value for op in Op)


def _parse_predicates(raw: Sequence[dict[str, Any]] | None) -> tuple[Predicate, ...]:
    predicates = []
    for item in raw or []:
        field_name = str(item.get("field", "")).strip()
        if not field_name or "op" not in item:
            raise GraphContextError(
                "each where item needs 'field' and 'op' (plus 'value' unless "
                f"op is exists/missing); ops: {_OPS_LISTING}"
            )
        try:
            op = Op(str(item["op"]).strip().casefold())
        except ValueError:
            raise GraphContextError(
                f"unknown op {item['op']!r}; allowed: {_OPS_LISTING}"
            ) from None
        predicates.append(
            Predicate(
                field=field_name,
                op=op,
                value=normalize_value(item.get("value", "")),
            )
        )
    return tuple(predicates)


def _parse_order_by(raw: Sequence[str] | None) -> tuple[SortKey, ...]:
    keys = []
    for item in raw or []:
        parts = str(item).split()
        directions = {"asc": False, "desc": True}
        if len(parts) == 1:
            keys.append(SortKey(field=parts[0]))
        elif len(parts) == 2 and parts[1].casefold() in directions:
            keys.append(
                SortKey(field=parts[0], descending=directions[parts[1].casefold()])
            )
        else:
            raise GraphContextError(
                f"bad order_by entry {item!r}; each entry is 'field', "
                "'field asc', or 'field desc'"
            )
    return tuple(keys)


def _validate_query_type(services: Services, requested: str) -> Role | None:
    """Typo-check a query's type filter and resolve its role.

    The vocabulary is open, so accept anything the space registry knows,
    any role name, or any identifier a node in the graph actually carries;
    reject the rest with the known-types listing (errors are prompts). A
    known type with zero instances proceeds and honestly matches nothing.
    """
    wanted = requested.casefold()
    role = services.repository.role_for(requested)
    if role is None:
        role = next((r for r in Role if r.value.casefold() == wanted), None)
    known = {t.casefold() for t in services.repository.known_node_types()}
    if wanted in known or role is not None:
        return role
    for node in services.repository.graph.nodes():
        if any(i.casefold() == wanted for i in node_identifiers(node)):
            return node.role
    raise UnknownNodeType(requested, tuple(services.repository.known_node_types()))


def _parse_field_declarations(
    raw: dict[str, str] | None,
) -> dict[str, str] | None:
    """Normalize a ``create_missing_fields`` map (key -> format); format
    well-formedness is the writer's rule (schema.validate_field_declarations)."""
    if raw is None:
        return None
    return {str(k).strip(): str(v).strip().lower() for k, v in raw.items()}


def _node_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_node_type(v) for v in values)


def _edge_type_set(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(_parse_edge_type(v) for v in values)


def _parse_hold_detail(value: str) -> Detail:
    normalized = value.strip().casefold()
    levels = {
        "": Detail.SUMMARIES,  # default bucket
        "summary": Detail.SUMMARIES,
        "summaries": Detail.SUMMARIES,
        "full": Detail.FULL,
    }
    if normalized not in levels:
        raise GraphContextError(
            f"unknown hold detail {value!r}; allowed: summaries (default), full"
        )
    return levels[normalized]
