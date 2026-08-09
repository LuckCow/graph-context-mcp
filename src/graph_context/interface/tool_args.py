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

from graph_context.domain import fields as domain_fields
from graph_context.domain.models import LinkSpec, NodeId, PropertyDeclaration
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
        return services.repository.graph.resolve(
            identifier, include_roles=services.visible_infra_roles
        ).id
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


def _parse_property_declarations(
    raw: dict[str, Any] | None,
) -> dict[str, PropertyDeclaration]:
    """Normalize ``create_missing_properties`` (ADR 042).

    Two spellings per entry: the string shorthand ``{"key": "format"}``
    (scope ``instance``) and the full form ``{"key": {"format": ...,
    "scope": "instance"|"type", "name": <optional display name>}}``.
    Per-declaration invariants (format/scope vocabulary, gc_ prefix) are
    :class:`PropertyDeclaration`'s own, raised at construction.
    """
    declarations: dict[str, PropertyDeclaration] = {}
    for raw_key, value in (raw or {}).items():
        key = str(raw_key).strip()
        if isinstance(value, str):
            declarations[key] = PropertyDeclaration(key=key, format=value)
        elif isinstance(value, dict):
            declarations[key] = PropertyDeclaration(
                key=key,
                format=str(value.get("format", "")),
                scope=str(value.get("scope", "instance") or "instance"),
                name=str(value.get("name", "")),
            )
        else:
            raise GraphContextError(
                f"create_missing_properties entry {key!r} must be a format "
                "string (scope 'instance') or {'format': ..., 'scope': "
                "'instance'|'type'}"
            )
    return declarations


def _coerce_property_value(key: str, value: Any) -> str:
    """One scalar ``properties`` value as its canonical string (ADR 042).

    The tool schema says strings, but models send what JSON offers --
    ``true`` for a checkbox, ``42`` for a number, a list for a
    multi_select -- and a type crash here used to surface as an opaque
    internal error (turn de38192f56dc). Coerce the unambiguous cases;
    reject the rest loudly, naming the property.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):  # before int: bool subclasses int
        return "true" if value else "false"
    if isinstance(value, int | float):
        return domain_fields.render_number(float(value))
    if isinstance(value, list | tuple):
        items = [item for item in value if str(item).strip()]
        if all(isinstance(item, str) for item in items):
            return ", ".join(item.strip() for item in items)
        raise GraphContextError(
            f"property {key!r} got a list with non-string entries; a "
            "multi_select takes strings (a relation takes node ids/names)"
        )
    raise GraphContextError(
        f"property {key!r} got an unusable {type(value).__name__} value; "
        "pass a string (checkbox: 'true'/'false'; multi_select: a comma "
        "list or list of strings; relation: a node id/name or list of them)"
    )


async def _relation_targets(
    services: Services, key: str, value: Any
) -> list[NodeId]:
    """A relation-valued ``properties`` entry: one node id/name, or a
    list of them, each resolved like any node reference."""
    items = list(value) if isinstance(value, list | tuple) else [value]
    targets: list[NodeId] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise GraphContextError(
                f"relation {key!r} takes a node id or name (or a list of "
                f"them); got {item!r}"
            )
        targets.append(await _resolve(services, item))
    return targets


async def _parse_properties(
    services: Services,
    properties: dict[str, Any] | None,
    create_missing: dict[str, Any] | None,
    *,
    on_type: str | None = None,
    on_node: NodeId | None = None,
) -> tuple[dict[str, str], list[LinkSpec], dict[str, PropertyDeclaration]]:
    """Split a write's ``properties`` dict into scalars and links (ADR 042).

    One surface, discriminated by what the target TYPE (plus, on update,
    the object itself -- the ``on_type``/``on_node`` scope, ADR 047) says
    the key IS: a key naming a relation the scope admits becomes link(s)
    -- its value resolves like a node reference, and any declaration for
    it is dropped (nothing may mint a scalar shadow of an edge, ADR 006);
    a key whose declaration says ``objects`` becomes link(s) -- reusing a
    matching space relation or minting one; every other key is a scalar
    value (coerced to its canonical string). Unadmitted undeclared keys
    stay in the scalars -- the REPOSITORY owns the approval error, not
    this boundary.
    """
    declarations = _parse_property_declarations(create_missing)
    scalars: dict[str, str] = {}
    links: list[LinkSpec] = []
    seen: set[tuple[str, NodeId]] = set()

    async def _add_links(label: str, key: str, value: Any) -> None:
        for target in await _relation_targets(services, key, value):
            coerced = (label.strip().lower(), target)
            if coerced not in seen:
                seen.add(coerced)
                links.append(LinkSpec(edge_type=label, other=target))

    for raw_key, value in (properties or {}).items():
        key = str(raw_key).strip()
        if not key:
            raise GraphContextError("properties has an entry with an empty key")
        label = services.repository.relation_label_for(
            key, on_type=on_type, on_node=on_node
        )
        if label is not None:
            declarations.pop(key, None)  # the relation already exists
            await _add_links(label, key, value)
            continue
        declaration = declarations.get(key)
        if declaration is not None and declaration.format == "objects":
            await _add_links(key, key, value)
            continue
        scalars[key] = _coerce_property_value(key, value)
    return scalars, links, declarations


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
    known = {
        t.casefold()
        for t in services.repository.known_node_types(
            services.visible_infra_roles
        )
    }
    if wanted in known or role is not None:
        return role
    for node in services.repository.graph.nodes():
        if any(i.casefold() == wanted for i in node_identifiers(node)):
            return node.role
    raise UnknownNodeType(
        requested,
        tuple(
            services.repository.known_node_types(services.visible_infra_roles)
        ),
    )


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
