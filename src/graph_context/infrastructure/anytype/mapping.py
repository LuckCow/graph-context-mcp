"""Mapping: Node/Edge <-> Anytype object translation.

THE QUIRK QUARANTINE. Every assumption about how Anytype represents our
schema lives in this module (and is mirrored by the mock server). When the
live-server spike corrects an assumption, this is the only production
module that should need to change.

Representation decisions (ADR-003 + WP1):

* One Anytype **Type** per :class:`NodeType`, key ``gc_<snake_case>``
  (``gc_character``, ``gc_session_context``, ...). The ``gc_`` prefix
  avoids collisions with user-created types/properties in a shared space.
* One relation ("objects" format) **Property** per :class:`EdgeType`, key
  ``gc_edge_<value>``, stored on the edge's **source** object and holding
  the list of target ids. Reverse adjacency exists only in the in-memory
  index.
* Scalar fields are properties: ``gc_summary`` (text), ``gc_summary_stale``
  (checkbox), ``gc_description`` (text), ``gc_story_time`` (number).
  ``Node.fields`` is serialized as a JSON blob in ``gc_fields`` (text) --
  good enough for v1's free-form extras.
* Object name maps to Anytype's top-level ``name``. Body is reserved for
  Prose text (WP3) and never carries structured data.

SPIKE-CONFIRMED against a live server (API 2025-11-08, 2026-06-21):
  A1. Relation properties round-trip through create/PATCH. CONFIRMED.
  A2. List/search responses include property values inline. CONFIRMED.
  A3. Property value envelope is ``{"key", "format", <format-named field>}``
      on reads; on *writes* the ``"format"`` field is optional (the server
      accepts and ignores it), so the entries built here work as-is.
  A4. PATCH with a property entry REPLACES that property's value wholesale
      (multi-value relations included) -- hence read-modify-write upstream.
      CONFIRMED (sent a one-element list, the prior targets were dropped).

  Timestamps (spike S3): ``created_date`` and ``last_modified_date`` are
  ``date``-format *properties*, not top-level fields, and
  ``last_modified_date`` is **absent until an object is first modified**.
  Sync therefore reads the "effective" stamp (``last_modified_date`` if
  present, else ``created_date``); see :func:`effective_modified`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from graph_context.domain.models import Edge, Node, NodeDraft, NodeId
from graph_context.domain.schema import EdgeType, NodeType

logger = logging.getLogger(__name__)

GC_PREFIX = "gc_"

PROP_SUMMARY = "gc_summary"
PROP_SUMMARY_STALE = "gc_summary_stale"
PROP_DESCRIPTION = "gc_description"
PROP_STORY_TIME = "gc_story_time"
PROP_FIELDS = "gc_fields"

# Anytype built-in timestamp properties (date format), used by sync. These are
# not ours to write -- read-only. ``last_modified_date`` appears only once an
# object has been modified at least once (spike S3).
PROP_LAST_MODIFIED = "last_modified_date"
PROP_CREATED = "created_date"

SCALAR_PROPERTIES: dict[str, str] = {  # key -> format
    PROP_SUMMARY: "text",
    PROP_SUMMARY_STALE: "checkbox",
    PROP_DESCRIPTION: "text",
    PROP_STORY_TIME: "number",
    PROP_FIELDS: "text",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


TYPE_KEYS: dict[NodeType, str] = {t: f"{GC_PREFIX}{_snake(t.value)}" for t in NodeType}
NODE_TYPES_BY_KEY: dict[str, NodeType] = {v: k for k, v in TYPE_KEYS.items()}

EDGE_PROPERTY_KEYS: dict[EdgeType, str] = {
    e: f"{GC_PREFIX}edge_{e.value}" for e in EdgeType
}
EDGE_TYPES_BY_PROPERTY: dict[str, EdgeType] = {
    v: k for k, v in EDGE_PROPERTY_KEYS.items()
}

# Every type key we own -- used to scope resync's search to our objects.
ALL_TYPE_KEYS: list[str] = list(TYPE_KEYS.values())

_VALUE_FIELD = {
    "text": "text",
    "number": "number",
    "checkbox": "checkbox",
    "objects": "objects",
    "date": "date",  # read-only here; lets _property_map surface timestamps
}


# -- outbound: domain -> API payloads -----------------------------------


def property_entry(key: str, fmt: str, value: Any) -> dict[str, Any]:
    return {"key": key, "format": fmt, _VALUE_FIELD[fmt]: value}


def to_create_payload(
    draft: NodeDraft, outgoing: Mapping[EdgeType, Sequence[NodeId]] = {}
) -> dict[str, Any]:
    """Build the POST body for a node plus its initial outgoing relations."""
    properties = [
        property_entry(PROP_SUMMARY, "text", draft.summary),
        property_entry(PROP_SUMMARY_STALE, "checkbox", False),
        property_entry(PROP_DESCRIPTION, "text", draft.description),
        property_entry(PROP_FIELDS, "text", json.dumps(dict(draft.fields))),
    ]
    if draft.story_time is not None:
        properties.append(property_entry(PROP_STORY_TIME, "number", draft.story_time))
    for edge_type, targets in outgoing.items():
        properties.append(
            property_entry(EDGE_PROPERTY_KEYS[edge_type], "objects", list(targets))
        )
    return {"name": draft.name, "type_key": TYPE_KEYS[draft.type], "properties": properties}


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
    edge_type: EdgeType, targets: Sequence[NodeId]
) -> dict[str, Any]:
    """PATCH body that REPLACES one relation property's target list (A4)."""
    return {
        "properties": [
            property_entry(EDGE_PROPERTY_KEYS[edge_type], "objects", list(targets))
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


def is_gc_object(obj: Mapping[str, Any]) -> bool:
    return (obj.get("type") or {}).get("key", "") in NODE_TYPES_BY_KEY


def to_node(obj: Mapping[str, Any]) -> Node | None:
    """Translate an API object to a :class:`Node`; ``None`` if not ours."""
    if obj.get("archived") or not is_gc_object(obj):
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
        type=NODE_TYPES_BY_KEY[obj["type"]["key"]],
        name=obj.get("name", ""),
        summary=props.get(PROP_SUMMARY) or "",
        summary_stale=bool(props.get(PROP_SUMMARY_STALE)),
        description=props.get(PROP_DESCRIPTION) or "",
        story_time=props.get(PROP_STORY_TIME),
        fields=fields,
    )


def to_edges(obj: Mapping[str, Any]) -> list[Edge]:
    """Extract the outgoing edges encoded in an object's relation properties."""
    edges: list[Edge] = []
    for entry in obj.get("properties", []):
        edge_type = EDGE_TYPES_BY_PROPERTY.get(entry.get("key", ""))
        if edge_type is None:
            continue
        for target in entry.get("objects") or []:
            edges.append(Edge(source=obj["id"], type=edge_type, target=target))
    return edges


# -- sync helpers: timestamps & the modified-since query -----------------


def effective_modified(obj: Mapping[str, Any]) -> str:
    """The object's change time as a sortable ISO string, or ``""``.

    ``last_modified_date`` if the server has surfaced it, else
    ``created_date`` (spike S3: the former is omitted until an object is
    first modified, but the server still filters/sorts by it internally,
    treating creation time as the modification time). Both are server-clock
    timestamps, so watermark comparisons stay immune to local clock skew.
    """
    props = _property_map(obj)
    return str(props.get(PROP_LAST_MODIFIED) or props.get(PROP_CREATED) or "")


def modified_since_filter(watermark: str) -> dict[str, Any]:
    """A ``POST /search`` filter selecting objects changed at/after ``watermark``.

    Uses ``>=`` (spike S3: seconds granularity, so re-processing the boundary
    second is unavoidable -- and harmless, applying changes is idempotent).
    """
    return {
        "property_key": PROP_LAST_MODIFIED,
        "condition": "greater_or_equal",
        "value": watermark,
    }
