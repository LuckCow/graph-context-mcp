"""AnytypeViewCatalog: compile Set views into NodeQuery values (WP13/ADR 018).

The store is a VIEW-DEFINITION SOURCE, never a second query engine: this
module reads each set's views (spike S9 shapes) and translates them into
:class:`NodeQuery`, which runs on the same in-memory engine as every
other read. Representation quirks live here, the view analogue of
``mapping.py``:

    V1. Sets are ordinary searchable objects (``types=["set"]``); the
        API can create one but NOT its query source, filters, or sorts
        -- those are desktop-only, and views have no write endpoints.
    V2. A view's ``property_key`` is camelCase for built-ins
        (``dueDate``, ``lastModifiedDate``) and the raw internal
        property ID (hex) for user-created properties. Translation:
        property-id map from ``GET /properties``, else a
        camelCase->snake_case shim. The ``format`` field is always
        ``"text"`` -- useless, ignored.
    V3. "Checkbox is unchecked" arrives as ``condition "eq", value ""``
        -- absence, exactly ADR 018's neq-matches-absence idiom, so it
        compiles to ``done neq true`` (and ``neq ""`` to ``eq true``).
    V4. The set object does NOT expose its source type; it is inferred
        by sampling one object from the view's server-side execution.
        An empty view therefore cannot be compiled (a typeless query
        with absence-matching predicates would match the whole world)
        and is skipped with a log line.
    V5. A view with a condition (or value shape) we cannot translate is
        skipped, not mangled -- the catalog only lists views that run
        with the store's own meaning. ADR 018 keeps server-side
        execution as the documented fallback if V5 ever bites for real.
    V6. For API-created properties the view leaks Anytype's INTERNAL
        relation key (24-hex, e.g. ``6a4db893...``), which the REST
        surface never exposes (properties list shows the requested
        semantic key instead) -- live-confirmed unresolvable. An
        unresolvable SORT key is dropped with a log (ordering degrades
        by one tiebreaker); an unresolvable FILTER skips the view
        (dropping a filter would silently change which nodes match).
        Desktop-created properties are fine: their REST key IS the
        internal hex key.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from graph_context.domain.query import NodeQuery, Op, Predicate, SortKey, normalize_value
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeApiError
from graph_context.ports.view_catalog import SavedView

logger = logging.getLogger(__name__)

_SET_TYPE_KEYS = ("set",)  # V1: discovery via type-scoped search

# View filter conditions -> engine ops. Anytype's vocabulary observed
# live (S3 search grammar + S9 view defs); unknown conditions skip the
# view (V5).
_CONDITIONS: dict[str, Op] = {
    "eq": Op.EQ,
    "equal": Op.EQ,
    "neq": Op.NEQ,
    "not_equal": Op.NEQ,
    "greater": Op.GT,
    "less": Op.LT,
    "greater_or_equal": Op.GTE,
    "less_or_equal": Op.LTE,
    "like": Op.CONTAINS,
    "contains": Op.CONTAINS,
    "empty": Op.MISSING,
    "not_empty": Op.EXISTS,
    "exists": Op.EXISTS,
}

_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")
# An internal relation key (V6): 24 hex chars, unresolvable over REST
# unless it IS some desktop-created property's key.
_INTERNAL_KEY = re.compile(r"^[0-9a-f]{24}$")


class ViewCompileError(Exception):
    """Internal: this view cannot be represented as a NodeQuery (V5)."""


def _snake(key: str) -> str:
    return _CAMEL.sub(lambda m: "_" + m.group(1), key).lower()


def compile_view(
    view: dict[str, Any],
    *,
    node_type: str,
    key_of: dict[str, str],
    known_keys: frozenset[str] = frozenset(),
) -> NodeQuery:
    """One S9-shaped view definition -> NodeQuery (quirks V2/V3/V5/V6).

    ``key_of`` maps property ids -> keys (V2); ``known_keys`` is the
    space's real property-key vocabulary, used to spot internal keys
    that resolve to nothing (V6).
    """
    predicates = []
    for leaf in view.get("filters") or []:
        field = _field_key(leaf, key_of)
        if _unresolved(field, known_keys):
            # V6: dropping a filter would silently change the match set.
            raise ViewCompileError(
                f"filter on unresolvable internal property key {field!r}"
            )
        condition = str(leaf.get("condition", ""))
        op = _CONDITIONS.get(condition)
        if op is None:
            raise ViewCompileError(f"unsupported filter condition {condition!r}")
        raw_value = leaf.get("value", "")
        if isinstance(raw_value, (list, dict)):
            raise ViewCompileError(f"unsupported filter value shape {raw_value!r}")
        value = normalize_value(raw_value)
        if leaf.get("format") == "checkbox" and value == "":
            # V3: (un)checked-ness travels as (in)equality with absence.
            if op is Op.EQ:
                op, value = Op.NEQ, "true"
            elif op is Op.NEQ:
                op, value = Op.EQ, "true"
        predicates.append(Predicate(field=field, op=op, value=value))
    order_by = []
    for sort in view.get("sorts") or []:
        field = _field_key(sort, key_of)
        if _unresolved(field, known_keys):
            # V6: ordering degrades by one tiebreaker, honestly logged.
            logger.info(
                "view %r: dropping sort on unresolvable internal key %r",
                view.get("name"), field,
            )
            continue
        order_by.append(SortKey(
            field=field,
            descending=str(sort.get("sort_type", "asc")).lower() == "desc",
        ))
    return NodeQuery(
        node_type=node_type,
        predicates=tuple(predicates),
        order_by=tuple(order_by),
    )


def _unresolved(field: str, known_keys: frozenset[str]) -> bool:
    return bool(_INTERNAL_KEY.match(field)) and field not in known_keys


def _field_key(leaf: dict[str, Any], key_of: dict[str, str]) -> str:
    raw = str(leaf.get("property_key", ""))
    if not raw:
        raise ViewCompileError("filter/sort with no property_key")
    return key_of.get(raw, _snake(raw))


class AnytypeViewCatalog:
    """``ViewCatalog`` over the live lists/views endpoints."""

    def __init__(self, client: AnytypeClient) -> None:
        self._client = client

    async def load(self) -> tuple[SavedView, ...]:
        key_of: dict[str, str] = {}
        known_keys = set()
        async for p in self._client.list_properties():
            if p.get("id") and p.get("key"):
                key_of[p["id"]] = p["key"]
                known_keys.add(p["key"])
        views: list[SavedView] = []
        async for set_object in self._client.search(types=list(_SET_TYPE_KEYS)):
            set_id = str(set_object["id"])
            set_name = str(set_object.get("name") or "(unnamed set)")
            async for view in self._client.list_views(set_id):
                view_id = str(view.get("id", ""))
                view_name = str(view.get("name") or view_id)
                try:
                    node_type = await self._source_type(set_id, view_id)
                    query = compile_view(
                        view, node_type=node_type, key_of=key_of,
                        known_keys=frozenset(known_keys),
                    )
                except ViewCompileError as err:
                    logger.info(
                        "skipping view %s/%s: %s", set_name, view_name, err
                    )
                    continue
                except AnytypeApiError as err:
                    # A SOURCELESS set's execution endpoint 500s (S9);
                    # any per-view API failure skips the view, not the
                    # catalog -- other sets must stay listable.
                    logger.info(
                        "skipping view %s/%s: %s", set_name, view_name, err
                    )
                    continue
                views.append(SavedView(
                    set_name=set_name, view_name=view_name, query=query,
                    set_id=set_id, view_id=view_id,
                ))
        return tuple(views)

    async def _source_type(self, set_id: str, view_id: str) -> str:
        """Infer the set's source type by sampling its execution (V4)."""
        sample = await self._client.sample_view_objects(set_id, view_id, limit=1)
        if not sample:
            raise ViewCompileError(
                "empty view -- the set's source type cannot be inferred"
            )
        type_key = str((sample[0].get("type") or {}).get("key", ""))
        if not type_key:
            raise ViewCompileError("sampled object carries no type key")
        return type_key
