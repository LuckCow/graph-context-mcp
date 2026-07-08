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
  also backs inline ``anytype://`` body links) is read as a generic edge,
  but only for targets no named relation on the same object already points
  at -- Anytype mirrors semantic connections into ``links``, and reading
  the mirror verbatim would double every edge (see :func:`to_edges`).
* Scalar fields we own: the **summary** lives in Anytype's built-in
  ``description`` property (ADR 011 -- UI-featured, present in list/search
  so it hydrates); ``gc_summary_stale`` (checkbox), ``gc_story_time``
  (number), and the ``gc_fields`` JSON blob remain ``gc_`` properties
  written onto the native object. Object name maps to Anytype's top-level
  ``name``.
* The node's long-form **description is the object body** (ADR 010).
  Created via the ``body`` key, read back as ``markdown``, updated via the
  ``markdown`` key in PATCH (**A7**: a wholesale replace, combinable with
  name/properties in one PATCH; a ``body`` key in PATCH is silently
  ignored -- the documented create/update field-name mismatch). Bodies are
  absent from list/search responses, so they never enter the index;
  :func:`body_of` is the single read. Pre-ADR-010 spaces are converted by
  ``scripts/migrate_descriptions_to_body.py``.
* **A8:** the markdown export *prepends* the built-in ``description``
  property (the summary, ADR 011) as its first line, but PATCH writes
  body blocks only -- GET -> PATCH round-trips would duplicate the
  summary line. :func:`body_of` strips the prefix; write-backs must
  write its output, never the raw ``markdown`` field.

SPIKE-CONFIRMED against a live server (API 2025-11-08): see git history. The
A1-A5 relation/PATCH assumptions are unchanged; A6 ("bodies are write-once")
was corrected to A7 on 2026-07-02 (body patching is a documented feature of
this API version; the original spike used the wrong field name). Only the
*which keys* question moved from a closed ``gc_`` set to "whatever the space
has".

  Timestamps (spike S3): ``created_date`` and ``last_modified_date`` are
  ``date``-format *properties*, not top-level fields, and
  ``last_modified_date`` is absent until an object is first modified; sync
  reads the "effective" stamp (see :func:`effective_modified`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from graph_context.domain.models import Edge, Node, NodeDraft, NodeId

if TYPE_CHECKING:
    from graph_context.infrastructure.anytype.registry import SpaceRegistry

logger = logging.getLogger(__name__)

GC_PREFIX = "gc_"
GC_EDGE_PREFIX = "gc_edge_"

# The summary channel IS Anytype's built-in description property (ADR 011):
# a one-liner slot the UI features under titles, in Set rows, and in
# previews -- and, unlike the body (A7), present in list/search responses,
# so summaries ride the hydrate sweep. Not to be confused with the tool
# surface's "description", which is the long-form body (ADR 010).
PROP_SUMMARY = "description"
PROP_SUMMARY_STALE = "gc_summary_stale"
PROP_STORY_TIME = "gc_story_time"  # the DEFAULT timeline property (ADR 015)
PROP_FIELDS = "gc_fields"

# The timeline source is profile-declared (ADR 015): fiction keeps the
# gc_story_time number; a date-axis profile names a native date property
# (ISO strings order lexicographically). One representation per space.
DEFAULT_TIMELINE: tuple[str, str] = (PROP_STORY_TIME, "number")

# Retired keys. Each survives only for its migration script under
# scripts/; nothing in the server reads or writes them.
# - gc_description (ADR 010): long-form text moved to the object body.
# - gc_summary (ADR 011): the one-liner moved to the built-in description.
PROP_LEGACY_DESCRIPTION = "gc_description"
PROP_LEGACY_SUMMARY = "gc_summary"

# Anytype built-in timestamp properties (date format), used by sync. Read-only.
PROP_LAST_MODIFIED = "last_modified_date"
PROP_CREATED = "created_date"

SCALAR_PROPERTIES: dict[str, str] = {  # key -> format; bootstrap mints these
    # PROP_SUMMARY is absent deliberately: the built-in description property
    # exists in every space (ADR 011) -- nothing to mint.
    PROP_SUMMARY_STALE: "checkbox",
    PROP_STORY_TIME: "number",
    PROP_FIELDS: "text",
}

# Activity Mode config objects (ADR 015 amendment): the human-editable
# fields of a gc_activity_mode object. Kept OUT of SCALAR_PROPERTIES --
# these live only on mode objects, never on ordinary nodes. The goal is
# the object BODY (read via body_of), so it needs no property.
PROP_MODE_MUTATING = "gc_mode_mutating"
PROP_CAPTURE_TYPE = "gc_capture_type"
PROP_CAPTURE_REFERENCES = "gc_capture_references"
PROP_CAPTURE_MIN_CHARS = "gc_capture_min_chars"

MODE_PROPERTIES: dict[str, str] = {  # key -> format; bootstrap mints these
    PROP_MODE_MUTATING: "checkbox",
    PROP_CAPTURE_TYPE: "text",
    PROP_CAPTURE_REFERENCES: "text",
    PROP_CAPTURE_MIN_CHARS: "number",
}

# Session discriminator (WP8, ADR 021): every gc_session_context node
# carries the transport-scoped session key it belongs to (e.g.
# "anytype:<chat_id>", "mcp"). Kept OUT of SCALAR_PROPERTIES -- it lives
# only on session nodes, never on ordinary nodes.
PROP_SESSION_KEY = "gc_session_key"
SESSION_PROPERTIES: dict[str, str] = {  # key -> format; bootstrap mints these
    PROP_SESSION_KEY: "text",
}

# Anytype's generic inline-link relation: an object's outbound ``anytype://``
# body links are mirrored here, so reading it surfaces inline links as edges.
GENERIC_LINK_KEY = "links"

# -- native scalar reflection (ADR 012) -----------------------------------

# Property formats that surface in Node.fields (and are writable through
# the ``fields`` parameter). ``objects`` is edges; everything else scalar.
REFLECTED_FIELD_FORMATS: frozenset[str] = frozenset(
    {"text", "number", "select", "multi_select", "date", "checkbox",
     "url", "email", "phone"}
)

# System properties that would be pure context-window noise if reflected.
# Census-based (real space, 2026-07-02): every object carries these. A
# curated list, deliberately NOT a name heuristic -- `creator_origin`
# looked system-flavored and turned out to be a user's story relation.
# Space-specific additions come from GC_FIELD_DENYLIST via the registry.
SYSTEM_PROPERTY_DENYLIST: frozenset[str] = frozenset(
    {"created_date", "last_modified_date", "added_date",
     "last_opened_date", "last_used_date"}
)

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
    "date": "date",
    "select": "select",            # value: inline tag envelope (ADR 012)
    "multi_select": "multi_select",  # value: list of tag envelopes
    "url": "url",
    "email": "email",
    "phone": "phone",
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
    native_properties: Sequence[dict[str, Any]] = (),
    fields_blob: Mapping[str, str] | None = None,
    timeline: tuple[str, str] = DEFAULT_TIMELINE,
) -> dict[str, Any]:
    """Build the POST body for a node's system properties only.

    ``type_key`` is the resolved native Anytype type. Outgoing relations are
    *not* inlined here: a freshly-created relation is not yet attached to the
    object's type, so ``POST /objects`` would reject it with ``unknown property
    key``. The repository writes outgoing relations via a follow-up PATCH (which
    tolerates any space-level property), mirroring the update path.

    ADR 012 field routing: ``native_properties`` carries the already-resolved
    entries for ``fields`` keys that matched native scalar properties (select
    values resolved to existing tags -- inline select entries are validated
    by POST, so resolution must precede it); ``fields_blob`` is the residual
    written to ``gc_fields`` (defaults to all of ``draft.fields``).
    ``timeline`` is the profile-declared (key, format) the story_time value
    writes to (ADR 015).
    """
    blob = dict(draft.fields) if fields_blob is None else dict(fields_blob)
    properties = [
        property_entry(PROP_SUMMARY, "text", draft.summary),
        property_entry(PROP_SUMMARY_STALE, "checkbox", False),
        property_entry(PROP_FIELDS, "text", json.dumps(blob)),
        *native_properties,
    ]
    if draft.story_time is not None:
        properties.append(property_entry(timeline[0], timeline[1], draft.story_time))
    payload: dict[str, Any] = {
        "name": draft.name,
        "type_key": type_key,
        "properties": properties,
    }
    if draft.body:
        payload["body"] = draft.body  # A5/A7: `body` on create, `markdown` on update
    if draft.icon:
        # Emoji icon envelope (live-confirmed). Create-only by design:
        # icons are human-owned cosmetics after birth.
        payload["icon"] = {"format": "emoji", "emoji": draft.icon}
    return payload


def to_update_payload(
    *,
    name: str | None = None,
    summary: str | None = None,
    summary_stale: bool | None = None,
    body: str | None = None,
    story_time: float | str | None = None,
    fields: Mapping[str, str] | None = None,
    native_properties: Sequence[dict[str, Any]] = (),
    timeline: tuple[str, str] = DEFAULT_TIMELINE,
) -> dict[str, Any]:
    """Build a PATCH body containing only the provided changes.

    A body change rides the same PATCH as name/properties under the
    ``markdown`` key (A7) -- one throttled write, wholesale replace, and an
    empty string clears the body. ``fields`` here is the residual blob after
    the repository routed native-matching keys into ``native_properties``
    (ADR 012).
    """
    properties: list[dict[str, Any]] = [*native_properties]
    if summary is not None:
        properties.append(property_entry(PROP_SUMMARY, "text", summary))
    if summary_stale is not None:
        properties.append(property_entry(PROP_SUMMARY_STALE, "checkbox", summary_stale))
    if story_time is not None:
        properties.append(property_entry(timeline[0], timeline[1], story_time))
    if fields is not None:
        properties.append(property_entry(PROP_FIELDS, "text", json.dumps(dict(fields))))
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if body is not None:
        payload["markdown"] = body
    if properties:
        payload["properties"] = properties
    return payload


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


def field_value(fmt: str, raw: Any) -> str:
    """Normalize a native scalar property value to the string ``fields``
    carry (ADR 012): select -> the option's display name, multi_select ->
    comma-joined names, checkbox -> "true"/"false", numbers untrailed."""
    if raw is None:
        return ""
    if fmt == "select":
        return str(raw.get("name", "")) if isinstance(raw, Mapping) else str(raw)
    if fmt == "multi_select":
        return ", ".join(
            str(t.get("name", "")) if isinstance(t, Mapping) else str(t)
            for t in raw
        )
    if fmt == "checkbox":
        return "true" if raw else "false"
    if fmt == "number" and isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def to_node(obj: Mapping[str, Any], registry: SpaceRegistry) -> Node | None:
    """Translate an API object to a :class:`Node`.

    Returns ``None`` only for archived objects or objects with no type key.
    Every other object is a first-class node -- the space-reflecting model
    sees the user's native objects, not just a ``gc_`` subset.

    ``fields`` merges two channels (ADR 012): the ``gc_fields`` blob (the
    bot's channel for keys with no native property) and every reflectable
    native scalar property, normalized to strings. Native wins on a key
    collision -- the human-visible surface is authoritative. Empty values
    and false checkboxes are skipped; noise is filtered by
    ``registry.reflects_field`` (system denylist + GC_FIELD_DENYLIST).
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
    for entry in obj.get("properties", []):
        key, fmt = entry.get("key", ""), entry.get("format", "")
        if not key or not registry.reflects_field(key, fmt):
            continue
        raw = entry.get(_VALUE_FIELD.get(fmt, ""))
        if fmt == "checkbox" and not raw:
            continue  # an unticked checkbox is absence, not a fact
        value = field_value(fmt, raw)
        if value:
            fields[key] = value
    return Node(
        id=obj["id"],
        type=registry.type_name(type_key),
        type_key=type_key,
        role=registry.role_for(type_key),
        name=obj.get("name", ""),
        summary=props.get(PROP_SUMMARY) or "",
        summary_stale=bool(props.get(PROP_SUMMARY_STALE)),
        story_time=props.get(registry.timeline_key),
        fields=fields,
        modified_at=effective_modified(obj),
    )


def body_of(obj: Mapping[str, Any]) -> str:
    """A fetched object's long-form body (its description; ADR 010).

    ``markdown`` is present only on single-object GET responses (A7). A
    space written before ADR 010 must run the migration script
    (``scripts/migrate_descriptions_to_body.py``) -- the retired
    ``gc_description`` property is not read here.

    **A8 (live-confirmed 2026-07-02):** the markdown *export* prepends the
    built-in ``description`` property (the summary channel, ADR 011) as
    the first line, while PATCH writes body blocks only -- so a naive
    GET -> PATCH round-trip duplicates the summary into the body. This
    function strips that prefix; every read goes through here, and any
    write-back path must therefore write ``body_of`` output, never the
    raw ``markdown`` field.

    The server-rendered connections footer (ADR 013) is likewise stripped:
    the LLM reads clean description text and gets edges from the graph.
    """
    return body_and_footer_of(obj)[0]


def body_and_footer_of(obj: Mapping[str, Any]) -> tuple[str, str]:
    """``(clean body, current footer)`` of a fetched object.

    The write-back seam: A8 prefix stripped, footer split off -- so a
    caller can compare/replace the footer and PATCH ``compose_body`` output
    without ever round-tripping export artifacts into the store.
    """
    markdown = str(obj.get("markdown", "") or "")
    summary = str(_property_map(obj).get(PROP_SUMMARY) or "")
    if summary and markdown.startswith(summary):
        remainder = markdown[len(summary):]
        stripped = remainder.lstrip(" \t")
        # Only a whole leading LINE is the export artifact; same text merely
        # prefixing the body's first paragraph is genuine content.
        if stripped == "" or stripped.startswith("\n"):
            markdown = stripped.removeprefix("\n")
    return split_connections_footer(markdown)


# -- the connections footer (ADR 013) -------------------------------------

CONNECTIONS_HEADING = "## Connections (auto)"

_HEADING_LINE = re.compile(r"^#{1,6}\s*Connections \(auto\)\s*$")
_RULE_LINE = re.compile(r"^\s*-{3,}\s*$")


def render_connections_footer(
    connections: Sequence[tuple[str, str, str]], space_id: str
) -> str:
    """The generated footer: one deep-linked line per OUTGOING relation.

    ``connections`` is ``(label, target name, target id)`` tuples, already
    ordered by the caller. Deep links are plain link marks -- clickable and
    PATCH-stable, never registered in links/backlinks (user-verified), so
    the footer can't mint edges. Empty input renders no footer at all.
    """
    if not connections:
        return ""
    lines = ["---", CONNECTIONS_HEADING]
    for label, name, target_id in connections:
        lines.append(
            f"- {label} → [{name}]"
            f"(anytype://object?objectId={target_id}&spaceId={space_id})"
        )
    return "\n".join(lines)


def split_connections_footer(markdown: str) -> tuple[str, str]:
    """``(body text, footer text)`` -- whitespace-tolerant (the store
    normalizes markdown, e.g. rewriting ``---`` with padding). Footer is
    ``""`` when none is present."""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if _HEADING_LINE.match(line.strip()):
            start = i
            if i > 0 and _RULE_LINE.match(lines[i - 1]):
                start = i - 1
            return "\n".join(lines[:start]).rstrip(), "\n".join(lines[start:]).strip()
    return markdown, ""


def strip_connections_footer(markdown: str) -> str:
    """Body text without the generated footer."""
    return split_connections_footer(markdown)[0]


def footers_equal(a: str, b: str) -> bool:
    """Content comparison across store normalization (per-line strip)."""

    def normalize(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    return normalize(a) == normalize(b)


def compose_body(clean_body: str, footer: str) -> str:
    """Body text + footer, ready for a ``markdown`` write (A7).

    Callers must pass ``body_of`` output as ``clean_body`` (A8: never the
    raw export) -- the server owns only the footer; text above it is
    written back verbatim.
    """
    if not footer:
        return clean_body
    if not clean_body.strip():
        return footer
    return f"{clean_body.rstrip()}\n\n{footer}"


def to_edges(obj: Mapping[str, Any]) -> list[Edge]:
    """Extract every outgoing edge encoded in an object's relation properties.

    An edge is any ``objects``-format relation not on the system denylist;
    the label is :func:`clean_label` of the property key. The label does not
    need the registry -- it is purely key-derived, which keeps it stable.

    The generic ``links`` relation is subordinate to semantic relations:
    Anytype mirrors inline body links there, so a target already reached by
    a named relation on this object would surface as a duplicate edge. A
    ``links`` edge is therefore emitted only for targets NO other relation
    on this object points at. Per-object visibility is enough (the mirror is
    same-source/same-direction), and sync re-derives an object's outgoing
    edges through this function, so removing the semantic relation in the
    Anytype UI resurrects the ``links``-only edge on the next resync.
    """
    source = obj["id"]
    relation_entries: list[tuple[str, list[str]]] = []
    semantic_targets: set[str] = set()
    for entry in obj.get("properties", []):
        if entry.get("format") != "objects":
            continue
        key = entry.get("key", "")
        if not key or key in SYSTEM_RELATION_DENYLIST:
            continue
        targets = list(entry.get("objects") or [])
        relation_entries.append((key, targets))
        if key != GENERIC_LINK_KEY:
            semantic_targets.update(targets)
    edges: list[Edge] = []
    for key, targets in relation_entries:
        label = clean_label(key)
        for target in targets:
            if key == GENERIC_LINK_KEY and target in semantic_targets:
                continue
            edges.append(Edge(source=source, type=label, target=target, property_key=key))
    return edges


def relation_targets(obj: Mapping[str, Any], property_key: str) -> list[str]:
    """Current targets of ONE ``objects``-format relation on a fetched object.

    The write-time read of ADR 009: PATCH payloads for relation lists are
    materialized from this (store truth) rather than from the index, so a
    wholesale-replace PATCH (A4) can never be built on stale state.
    """
    for entry in obj.get("properties", []):
        if entry.get("key") == property_key and entry.get("format") == "objects":
            return list(entry.get("objects") or [])
    return []


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
