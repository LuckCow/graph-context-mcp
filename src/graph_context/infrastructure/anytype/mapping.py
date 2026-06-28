"""Mapping: Node/Edge <-> Anytype object translation.

THE QUIRK QUARANTINE. Every assumption about how Anytype represents our
data lives in this module (and is mirrored by the mock server).

Representation (v2, space-reflecting):

* A node is **any** Anytype object. Its ``type`` is the object's native type
  (the user's own ``character``/``event``/... types -- we no longer mint a
  parallel ``gc_`` type per node kind). The type's display name and semantic
  Role are resolved through the :class:`SpaceRegistry`.
* An edge is **any** ``objects``-format relation property on the source
  object: bootstrapped ``gc_edge_*`` relations and human-created ones
  (``boss``, ``triggered_by``, ``key_members``...) alike. The edge's *label*
  is the relation key with the ``gc_edge_``/``gc_`` prefix stripped
  (:func:`clean_label`) -- key-derived so the label round-trips back to the
  exact property key on write and on filter. A small denylist drops
  account-pointing system relations; the generic ``links`` relation (which
  also backs inline ``anytype://`` body links) is read as a generic edge.
* Scalar fields we own are still ``gc_`` properties written onto the native
  object: ``gc_summary`` (text), ``gc_summary_stale`` (checkbox),
  ``gc_description`` (text), ``gc_story_time`` (number), and the ``gc_fields``
  JSON blob. Object name maps to Anytype's top-level ``name``; body is
  reserved for Prose text (write-once; A5/A6).

SPIKE-CONFIRMED against a live server (API 2025-11-08): see git history. The
A1-A6 relation/PATCH/body assumptions are unchanged; only the *which keys*
question moved from a closed ``gc_`` set to "whatever the space has".

  Timestamps (spike S3): ``created_date`` and ``last_modified_date`` are
  ``date``-format *properties*, not top-level fields, and
  ``last_modified_date`` is absent until an object is first modified; sync
  reads the "effective" stamp (see :func:`effective_modified`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from graph_context.domain.models import Edge, Node, NodeDraft, NodeId

if TYPE_CHECKING:
    from graph_context.infrastructure.anytype.registry import SpaceRegistry

logger = logging.getLogger(__name__)

GC_PREFIX = "gc_"
GC_EDGE_PREFIX = "gc_edge_"

PROP_SUMMARY = "gc_summary"
PROP_SUMMARY_STALE = "gc_summary_stale"
PROP_DESCRIPTION = "gc_description"
PROP_STORY_TIME = "gc_story_time"
PROP_FIELDS = "gc_fields"

# Anytype built-in timestamp properties (date format), used by sync. Read-only.
PROP_LAST_MODIFIED = "last_modified_date"
PROP_CREATED = "created_date"

SCALAR_PROPERTIES: dict[str, str] = {  # key -> format
    PROP_SUMMARY: "text",
    PROP_SUMMARY_STALE: "checkbox",
    PROP_DESCRIPTION: "text",
    PROP_STORY_TIME: "number",
    PROP_FIELDS: "text",
}

# Anytype's generic inline-link relation: an object's outbound ``anytype://``
# body links are mirrored here, so reading it surfaces inline links as edges.
GENERIC_LINK_KEY = "links"

# Account-pointing / reverse-adjacency system relations that are NOT story
# edges. ``backlinks`` is the reverse of ``links``/named relations and is
# already covered by the in-memory reverse index, so reading it would
# double-count; ``creator``/``last_modified_by`` point at the user account.
SYSTEM_RELATION_DENYLIST: frozenset[str] = frozenset(
    {"backlinks", "creator", "last_modified_by"}
)

_VALUE_FIELD = {
    "text": "text",
    "number": "number",
    "checkbox": "checkbox",
    "objects": "objects",
    "date": "date",  # read-only here; lets _property_map surface timestamps
}


def clean_label(key: str) -> str:
    """The canonical edge label for a relation property ``key``.

    Derived from the key (so the label round-trips back to the exact property
    on write) with the ``gc_edge_``/``gc_`` prefixes stripped:
    ``gc_edge_knows`` -> ``knows``, ``triggered_by`` -> ``triggered_by``,
    ``boss`` -> ``boss``.
    """
    if key.startswith(GC_EDGE_PREFIX):
        return key[len(GC_EDGE_PREFIX):]
    if key.startswith(GC_PREFIX):
        return key[len(GC_PREFIX):]
    return key


# -- outbound: domain -> API payloads -----------------------------------


def property_entry(key: str, fmt: str, value: Any) -> dict[str, Any]:
    return {"key": key, "format": fmt, _VALUE_FIELD[fmt]: value}


def to_create_payload(
    draft: NodeDraft,
    *,
    type_key: str,
) -> dict[str, Any]:
    """Build the POST body for a node's system properties only.

    ``type_key`` is the resolved native Anytype type. Outgoing relations are
    *not* inlined here: a freshly-created relation is not yet attached to the
    object's type, so ``POST /objects`` would reject it with ``unknown property
    key``. The repository writes outgoing relations via a follow-up PATCH (which
    tolerates any space-level property), mirroring the update path.
    """
    properties = [
        property_entry(PROP_SUMMARY, "text", draft.summary),
        property_entry(PROP_SUMMARY_STALE, "checkbox", False),
        property_entry(PROP_DESCRIPTION, "text", draft.description),
        property_entry(PROP_FIELDS, "text", json.dumps(dict(draft.fields))),
    ]
    if draft.story_time is not None:
        properties.append(property_entry(PROP_STORY_TIME, "number", draft.story_time))
    payload: dict[str, Any] = {
        "name": draft.name,
        "type_key": type_key,
        "properties": properties,
    }
    if draft.body:
        payload["body"] = draft.body  # A5: Markdown body, write-once (A6)
    return payload


def to_update_payload(
    *,
    name: str | None = None,
    summary: str | None = None,
    summary_stale: bool | None = None,
    description: str | None = None,
    story_time: float | None = None,
    fields: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a PATCH body containing only the provided changes."""
    properties: list[dict[str, Any]] = []
    if summary is not None:
        properties.append(property_entry(PROP_SUMMARY, "text", summary))
    if summary_stale is not None:
        properties.append(property_entry(PROP_SUMMARY_STALE, "checkbox", summary_stale))
    if description is not None:
        properties.append(property_entry(PROP_DESCRIPTION, "text", description))
    if story_time is not None:
        properties.append(property_entry(PROP_STORY_TIME, "number", story_time))
    if fields is not None:
        properties.append(property_entry(PROP_FIELDS, "text", json.dumps(dict(fields))))
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if properties:
        body["properties"] = properties
    return body


def relation_patch_payload(
    property_key: str, targets: Sequence[NodeId]
) -> dict[str, Any]:
    """PATCH body that REPLACES one relation property's target list (A4)."""
    return {"properties": [property_entry(property_key, "objects", list(targets))]}


def relations_patch_payload(
    outgoing: Mapping[str, Sequence[NodeId]],
) -> dict[str, Any]:
    """PATCH body that sets several relation properties at once.

    ``outgoing`` is keyed by relation *property key* (already resolved from
    labels by the repository). Used to attach a new node's outgoing relations
    after ``POST /objects`` creates the bare object."""
    return {
        "properties": [
            property_entry(key, "objects", list(targets))
            for key, targets in outgoing.items()
        ]
    }


# -- inbound: API objects -> domain --------------------------------------


def _property_map(obj: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in obj.get("properties", []):
        fmt = entry.get("format")
        field = _VALUE_FIELD.get(fmt)
        if field is not None:
            out[entry["key"]] = entry.get(field)
    return out


def to_node(obj: Mapping[str, Any], registry: SpaceRegistry) -> Node | None:
    """Translate an API object to a :class:`Node`.

    Returns ``None`` only for archived objects or objects with no type key.
    Every other object is a first-class node -- the space-reflecting model
    sees the user's native objects, not just a ``gc_`` subset.
    """
    type_key = (obj.get("type") or {}).get("key", "")
    if obj.get("archived") or not type_key:
        return None
    props = _property_map(obj)
    raw_fields = props.get(PROP_FIELDS) or "{}"
    try:
        fields = {str(k): str(v) for k, v in json.loads(raw_fields).items()}
    except (ValueError, AttributeError):
        logger.warning("unparseable gc_fields on %s; ignoring", obj.get("id"))
        fields = {}
    return Node(
        id=obj["id"],
        type=registry.type_name(type_key),
        type_key=type_key,
        role=registry.role_for(type_key),
        name=obj.get("name", ""),
        summary=props.get(PROP_SUMMARY) or "",
        summary_stale=bool(props.get(PROP_SUMMARY_STALE)),
        description=props.get(PROP_DESCRIPTION) or "",
        story_time=props.get(PROP_STORY_TIME),
        fields=fields,
    )


def to_edges(obj: Mapping[str, Any]) -> list[Edge]:
    """Extract every outgoing edge encoded in an object's relation properties.

    An edge is any ``objects``-format relation not on the system denylist;
    the label is :func:`clean_label` of the property key. The label does not
    need the registry -- it is purely key-derived, which keeps it stable.
    """
    source = obj["id"]
    edges: list[Edge] = []
    for entry in obj.get("properties", []):
        if entry.get("format") != "objects":
            continue
        key = entry.get("key", "")
        if not key or key in SYSTEM_RELATION_DENYLIST:
            continue
        label = clean_label(key)
        for target in entry.get("objects") or []:
            edges.append(Edge(source=source, type=label, target=target, property_key=key))
    return edges


# -- sync helpers: timestamps & the modified-since query -----------------


def effective_modified(obj: Mapping[str, Any]) -> str:
    """The object's change time as a sortable ISO string, or ``""``.

    ``last_modified_date`` if the server has surfaced it, else
    ``created_date`` (spike S3). Both are server-clock timestamps, so
    watermark comparisons stay immune to local clock skew.
    """
    props = _property_map(obj)
    return str(props.get(PROP_LAST_MODIFIED) or props.get(PROP_CREATED) or "")


def modified_since_filter(watermark: str) -> dict[str, Any]:
    """A ``POST /search`` filter selecting objects changed at/after ``watermark``."""
    return {
        "property_key": PROP_LAST_MODIFIED,
        "condition": "greater_or_equal",
        "value": watermark,
    }
