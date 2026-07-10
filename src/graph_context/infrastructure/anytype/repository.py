"""``AnytypeGraphRepository``: the production :class:`GraphRepository`.

Write ordering (port contract): persist to Anytype first, then mutate the
index -- the index may lag the store but never lead it. A failed API call
leaves the index untouched.

Space-reflecting resolution (v2): a write names a *type* and relation
*labels*, not a fixed vocabulary. The repository resolves them against the
live :class:`SpaceRegistry`:
  * the node's ``type`` -> an existing native type key (else ``UnknownNodeType``);
  * each link label -> an existing ``objects`` relation key (reuse), or, when
    ``create_missing_relations`` is set, a freshly created relation. An
    unknown label otherwise raises ``UnknownRelationLabel``.
Both resolutions happen *before* the node POST, so an approval error never
leaves a half-applied write.

Composite-create choreography (no transactions in the API):
  1. Resolve type + relation keys and pre-validate endpoints (index-only).
  2. POST the node with its *outgoing* relations inline (zero extra calls).
  3. For *incoming* links, PATCH each source object's relation property
     (read-modify-write from index state -- PATCH replaces lists, A4).
  4. On any failure after the POST: archive the created node and restore
     every already-patched source, then re-raise.

Concurrency stance (WP1): last-write-wins versus human edits; the
read-modify-write in step 3 reads from the *index*.
"""

from __future__ import annotations

import asyncio
import logging
import re
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from graph_context.domain import schema
from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import (
    Edge,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    TimelineValue,
)
from graph_context.domain.schema import Role
from graph_context.errors import (
    GraphContextError,
    UnknownNodeType,
    UnknownRelationLabel,
)
from graph_context.infrastructure.anytype import mapping, sync
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeApiError
from graph_context.infrastructure.anytype.registry import (
    PropertyInfo,
    SpaceRegistry,
    load_registry,
)

logger = logging.getLogger(__name__)

# Live finding (2026-07): a relation created via POST /properties is not
# immediately usable -- PATCHing it onto an object 400s with "unknown
# property key" for a short settle window (~0.5 s observed). Retried with
# backoff; total budget ~4.5 s, well past anything seen live.
_FRESH_KEY_ATTEMPTS = 5
_FRESH_KEY_BACKOFF_SECONDS = 0.3
_UNKNOWN_KEY_MARKER = "unknown property key"
# Freshly created select options appear to have the same settle window
# (inferred from a live flake -- an object write immediately after
# create_tag 400'd "invalid select option", then succeeded; not
# spike-reproducible once the tag exists). Same retry discipline.
_INVALID_OPTION_MARKERS = ("invalid select option", "invalid multi_select option")


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "relation"


# Anytype's tag palette. CreateTagRequest requires a color (live-confirmed);
# picking by name hash is deterministic without maintaining a mapping, and a
# human can recolor in the UI without us ever clobbering it (create-only).
_TAG_COLORS = ("grey", "yellow", "orange", "red", "pink",
               "purple", "blue", "ice", "teal", "lime")


def _tag_color(name: str) -> str:
    return _TAG_COLORS[zlib.crc32(name.strip().lower().encode()) % len(_TAG_COLORS)]


class AnytypeGraphRepository:
    """Write-through repository over the Anytype local API."""

    def __init__(
        self,
        client: AnytypeClient,
        *,
        role_overrides: Mapping[str, Role] | None = None,
        field_denylist: Iterable[str] = (),
        timeline: tuple[str, str] = mapping.DEFAULT_TIMELINE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._graph = GraphIndex()
        # Profile-supplied type-key -> Role additions (WP5); merged into the
        # registry on every (re)load so resync keeps them.
        self._role_overrides: dict[str, Role] = dict(role_overrides or {})
        # Space-specific field-reflection silences (GC_FIELD_DENYLIST,
        # ADR 012); merged with the system denylist inside the registry.
        self._field_denylist: frozenset[str] = frozenset(field_denylist)
        # Profile-declared (property key, format) for the Event timeline
        # (ADR 015); gc_story_time/number is the fiction default.
        self._timeline = timeline
        self._registry = SpaceRegistry(
            role_overrides=dict(self._role_overrides),
            hidden_field_keys=self._field_denylist,
            timeline_key=timeline[0],
        )
        # ADR 009: the single-writer seam. Every store mutation runs inside
        # this FIFO lock, and relation-list PATCH payloads are materialized
        # from a fresh GET *inside* the critical section -- so a wholesale-
        # replace PATCH (A4) is never built on state another writer already
        # changed. `pending_writes` is the queue-depth surface (WP8).
        self._write_lock = asyncio.Lock()
        self._pending_writes = 0
        # Relation keys created by this repository that have not yet been
        # proven usable in a PATCH (the live settle window; see module note).
        self._unsettled_keys: set[str] = set()
        # Tag keys created by this repository, same settle discipline.
        self._unsettled_tags: set[str] = set()
        self._watermark: str | None = None  # None until first hydrate
        # Self-write suppression: stamps already accounted for (our own writes
        # + hydrate), so resync's watermark query never reports our own
        # boundary write as an out-of-band change.
        self._seen_stamps: dict[NodeId, str] = {}
        # type_key -> template object id to apply on create (or None: no
        # template / infra). Negative entries are cached because most types
        # have none, so a create must not pay an extra GET on the hot path;
        # cleared whenever the registry rebuilds so a newly UI-authored
        # template appears.
        self._templates: dict[str, str | None] = {}

    @property
    def graph(self) -> GraphIndex:
        return self._graph

    @property
    def registry(self) -> SpaceRegistry:
        return self._registry

    @property
    def pending_writes(self) -> int:
        """Write operations queued or in flight (ADR 009 depth surface)."""
        return self._pending_writes

    # -- registry lookups (port surface) ----------------------------------

    def role_for(self, type_identifier: str) -> Role | None:
        key = self._registry.type_key_for(type_identifier)
        if key is not None:
            return self._registry.role_for(key)
        return schema.resolve_role(type_identifier, self._role_overrides)

    def known_node_types(self) -> frozenset[str]:
        return self._registry.known_node_types()

    def known_edge_labels(self) -> frozenset[str]:
        return self._registry.known_edge_labels()

    # -- sync -------------------------------------------------------------

    async def hydrate(self) -> None:
        self._registry = await load_registry(
            self._client, extra_role_overrides=self._role_overrides,
            hidden_field_keys=self._field_denylist,
            timeline_key=self._timeline[0],
        )
        self._templates.clear()  # registry rebuilt: re-resolve templates lazily
        self._graph, watermark, stamps = await sync.load_index(
            self._client, self._registry
        )
        self._watermark = watermark
        self._seen_stamps = stamps

    async def resync(self) -> frozenset[NodeId]:
        """Apply out-of-band changes; first call without hydrate = full load."""
        if self._watermark is None:
            await self.hydrate()
            return frozenset(node.id for node in self._graph.nodes())
        # Refresh the registry so human-created types/relations get labelled.
        self._registry = await load_registry(
            self._client, extra_role_overrides=self._role_overrides,
            hidden_field_keys=self._field_denylist,
            timeline_key=self._timeline[0],
        )
        self._templates.clear()  # registry rebuilt: re-resolve templates lazily
        fetched = await sync.fetch_changes(self._client, self._watermark)
        unseen = [
            obj for obj in fetched
            if mapping.effective_modified(obj)
            > self._seen_stamps.get(obj.get("id", ""), "")
        ]
        changed_ids, watermark = sync.apply_changes(
            self._graph, unseen, self._registry
        )
        for obj in unseen:
            self._seen_stamps[obj["id"]] = mapping.effective_modified(obj)
        self._watermark = max(self._watermark, watermark)
        if changed_ids:
            logger.info("resync applied %d out-of-band changes", len(changed_ids))
        return changed_ids

    async def fetch_body(self, node_id: NodeId) -> str:
        """On-demand body read (A5/A7). The only path that fetches one object."""
        self._graph.node(node_id)  # NodeNotFound before spending an API call
        obj = await self._client.get_object(node_id)
        return mapping.body_of(obj)

    async def _template_for(self, type_key: str) -> str | None:
        """The template object id to apply when creating this type, or None.

        Cached per type_key (including the negative -- most types have none).
        Applying a template gives new objects the type template's default
        property values + layout (the human "+"-button experience); our inline
        properties/body then override/append (spiked)."""
        if type_key in self._templates:
            return self._templates[type_key]
        chosen: str | None = None
        type_id = self._registry.type_id_for(type_key)
        if type_id:
            ids = [t["id"] async for t in self._client.list_templates(type_id)]
            chosen = self._choose_template(ids)
        self._templates[type_key] = chosen
        return chosen

    @staticmethod
    def _choose_template(ids: list[str]) -> str | None:
        """Pick which template to apply. The API exposes no "default" flag, so
        we take the first one it returns (the sole policy knob -- a per-type
        config override would live here)."""
        return ids[0] if ids else None

    # -- writes -----------------------------------------------------------

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        create_missing_relations: bool = False,
    ) -> Node:
        type_key = self._registry.type_key_for(draft.type)
        if type_key is None:
            raise UnknownNodeType(draft.type, tuple(self._registry.known_node_types()))

        # Apply the type's template on create (default property values + layout),
        # except for infra roles -- bot-owned bookkeeping whose bodies are
        # write-once and must not carry a human's UI scaffold.
        template_id: str | None = None
        if self._registry.role_for(type_key) not in schema.INFRA_ROLES:
            template_id = await self._template_for(type_key)

        # Pre-validate endpoints (index-only) before any API call.
        for link in links:
            self._graph.node(link.other)  # raises NodeNotFound

        async with self._writer():
            # Resolve every link label -> relation property key (may create
            # relations); raises UnknownRelationLabel before any persistence.
            resolved = [
                (link, await self._resolve_relation(link.edge_type, create_missing_relations))
                for link in links
            ]

            outgoing: dict[str, list[NodeId]] = {}
            incoming: list[tuple[LinkSpec, str]] = []
            for link, key in resolved:
                if link.outgoing:
                    outgoing.setdefault(key, []).append(link.other)
                else:
                    incoming.append((link, key))

            # Field routing (ADR 012): native-matching keys become property
            # entries (select tags resolved-or-created first -- POST
            # validates them inline); the residual goes to the blob.
            native_fields, blob = await self._resolve_field_entries(draft.fields)
            created = await self._send_tolerating_fresh_tags(
                lambda: self._client.create_object(
                    mapping.to_create_payload(
                        draft, type_key=type_key,
                        native_properties=native_fields, fields_blob=blob,
                        timeline=self._timeline,
                        template_id=template_id or "",
                    )
                )
            )
            node = mapping.to_node(created, self._registry)
            if node is None:  # defensive: the store returned something unusable
                raise GraphContextError(
                    f"created object {created.get('id')} did not map back to a node"
                )

            patched: list[tuple[NodeId, str, list[NodeId], str | None]] = []
            try:
                # Outgoing relations are PATCHed onto the new object rather than
                # inlined in the POST: a freshly-created relation is not yet on the
                # type, so an inline POST would 400. A failure here falls through to
                # the rollback, which archives the node (and with it these edges).
                if outgoing:
                    payload = mapping.relations_patch_payload(outgoing)
                    markdown = self._footer_markdown(
                        created, node,
                        [link.to_edge(anchor=node.id, property_key=key)
                         for link, key in resolved if link.outgoing],
                    )
                    if markdown is not None:
                        payload["markdown"] = markdown  # ADR 013, same PATCH
                    patched_self = await self._patch_relations(
                        node.id, payload, outgoing
                    )
                    self._track_watermark(patched_self)
                for link, key in incoming:
                    # Store-truth read (ADR 009): also makes the rollback
                    # restore what the store really held, not an index view.
                    obj, previous = await self._current_state(link.other, key)
                    payload = mapping.relation_patch_payload(key, [*previous, node.id])
                    markdown = self._footer_markdown(
                        obj, self._graph.node(link.other),
                        [*self._graph.edges(link.other, Direction.OUT),
                         link.to_edge(anchor=node.id, property_key=key)],
                        extra_names={node.id: node.name},
                    )
                    restore_markdown: str | None = None
                    if markdown is not None:
                        payload["markdown"] = markdown  # ADR 013, same PATCH
                        # What the rollback should write back (A8-clean).
                        restore_markdown = mapping.compose_body(
                            *mapping.body_and_footer_of(obj)
                        )
                    patched_source = await self._patch_relations(
                        link.other, payload, [key]
                    )
                    patched.append((link.other, key, previous, restore_markdown))
                    self._track_watermark(patched_source)
            except Exception:
                await self._rollback_create(node.id, patched)
                raise

            # Persisted everywhere -- now (and only now) mutate the index.
            self._graph.upsert_node(node)
            for link, key in resolved:
                self._graph.add_edge(link.to_edge(anchor=node.id, property_key=key))
            self._track_watermark(created)
            return node

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        summary_stale: bool | None = None,
        body: str | None = None,
        story_time: TimelineValue | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Node:
        existing = self._graph.node(node_id)  # NodeNotFound before any API call
        native_fields: list[dict[str, Any]] = []
        blob: Mapping[str, str] | None = None
        if fields is not None:
            native_fields, blob = await self._resolve_field_entries(fields)
        if body is not None and existing.role not in schema.INFRA_ROLES:
            # ADR 013: re-render the footer around the new text (index edges;
            # link changes maintain it on their own writes).
            footer = mapping.render_connections_footer(
                self._connections(self._graph.edges(node_id, Direction.OUT)),
                self._client.space_id,
            )
            body = mapping.compose_body(body, footer)
        payload = mapping.to_update_payload(
            name=name,
            summary=summary,
            summary_stale=summary_stale,
            body=body,
            story_time=story_time,
            fields=blob,
            native_properties=native_fields,
            timeline=self._timeline,
        )
        async with self._writer():
            updated = await self._send_tolerating_fresh_tags(
                lambda: self._client.update_object(node_id, payload)
            )
            node = mapping.to_node(updated, self._registry)
            if node is None:
                raise GraphContextError(f"updated object {node_id} did not map back")
            self._graph.upsert_node(node)
            self._track_watermark(updated)
            return node

    async def add_link(
        self, anchor: NodeId, link: LinkSpec, *, create_missing_relations: bool = False
    ) -> Edge:
        async with self._writer():
            key = await self._resolve_relation(link.edge_type, create_missing_relations)
            edge = link.to_edge(anchor=anchor, property_key=key)
            source = self._graph.node(edge.source)  # endpoints must exist
            self._graph.node(edge.target)
            obj, targets = await self._current_state(edge.source, key)
            if edge.target not in targets:
                payload = mapping.relation_patch_payload(key, [*targets, edge.target])
                markdown = self._footer_markdown(
                    obj, source,
                    [*self._graph.edges(edge.source, Direction.OUT), edge],
                )
                if markdown is not None:
                    payload["markdown"] = markdown  # ADR 013, same PATCH
                updated = await self._patch_relations(edge.source, payload, [key])
                self._track_watermark(updated)
            self._graph.add_edge(edge)
            return edge

    async def remove_link(self, edge: Edge) -> None:
        key = edge.property_key or self._registry.key_for_label(edge.type)
        if key is None:  # nothing on the store to patch; drop from the index
            self._graph.remove_edge(edge)
            return
        async with self._writer():
            obj, current = await self._current_state(edge.source, key)
            targets = [t for t in current if t != edge.target]
            payload = mapping.relation_patch_payload(key, targets)
            markdown = self._footer_markdown(
                obj, self._graph.node(edge.source),
                [e for e in self._graph.edges(edge.source, Direction.OUT) if e != edge],
            )
            if markdown is not None:
                payload["markdown"] = markdown  # ADR 013, same PATCH
            updated = await self._client.update_object(edge.source, payload)
            self._graph.remove_edge(edge)
            self._track_watermark(updated)

    # -- internals ----------------------------------------------------------

    @asynccontextmanager
    async def _writer(self) -> AsyncIterator[None]:
        """The single-writer critical section (ADR 009).

        FIFO over the lock's waiter queue: writes execute in arrival order,
        exactly one at a time. The settle-window retries in
        :meth:`_patch_relations` sleep while holding the lock -- deliberate,
        a later write must not overtake an earlier one mid-retry.
        """
        self._pending_writes += 1
        try:
            async with self._write_lock:
                yield
        finally:
            self._pending_writes -= 1

    async def _current_state(
        self, source: NodeId, property_key: str
    ) -> tuple[dict[str, Any], list[NodeId]]:
        """Store-truth object + targets of one relation, read at write time.

        Reads are unthrottled (S7), so the extra GET is cheap. This is also
        the precise Q2 race detector: divergence from the index view means a
        human edited this relation since the last (re)sync. The write then
        builds on store truth, so the human's edit SURVIVES instead of being
        clobbered by an index-derived wholesale-replace PATCH. The fetched
        object also carries the current markdown -- exactly what the
        connections footer (ADR 013) needs, for free.
        """
        obj = await self._client.get_object(source)
        current = mapping.relation_targets(obj, property_key)
        index_view = self._outgoing_targets(source, property_key)
        if set(current) != set(index_view):
            logger.warning(
                "out-of-band edit on %s.%s (index %s != store %s); "
                "building the write on store state so the edit survives",
                source, property_key, sorted(index_view), sorted(current),
            )
        return obj, current

    async def _current_targets(self, source: NodeId, property_key: str) -> list[NodeId]:
        _, current = await self._current_state(source, property_key)
        return current

    async def _patch_relations(
        self, object_id: NodeId, payload: dict[str, Any], keys: Iterable[str]
    ) -> dict[str, Any]:
        """``update_object`` tolerant of the fresh-relation settle window.

        A key created by :meth:`_resolve_relation` may 400 ("unknown property
        key") for a short while after creation; only then is the PATCH
        retried with backoff. Established keys fail fast as before.
        """
        fresh = self._unsettled_keys.intersection(keys)
        for attempt in range(_FRESH_KEY_ATTEMPTS):
            try:
                result = await self._client.update_object(object_id, payload)
            except AnytypeApiError as err:
                unsettled = (
                    fresh
                    and err.status == 400
                    and _UNKNOWN_KEY_MARKER in err.detail
                )
                if unsettled and attempt < _FRESH_KEY_ATTEMPTS - 1:
                    await self._sleep(_FRESH_KEY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise
            self._unsettled_keys -= fresh  # proven usable
            return result
        raise AssertionError("unreachable")  # loop always returns or raises

    async def _resolve_relation(self, label: str, create_missing: bool) -> str:
        """Resolve a relation label to an existing property key, reusing when
        possible. Surfaces unknown labels for approval unless ``create_missing``."""
        key = self._registry.key_for_label(label)
        if key is not None:
            return key
        if not create_missing:
            raise UnknownRelationLabel(
                label, tuple(self._registry.known_edge_labels())
            )
        created = await self._client.create_property(
            {"key": _slugify(label), "name": label, "format": "objects"}
        )
        info = PropertyInfo(
            key=created.get("key", _slugify(label)),
            name=created.get("name", label),
            format="objects",
        )
        self._registry.register_property(info)
        self._unsettled_keys.add(info.key)  # not yet PATCH-usable; see module note
        return info.key

    def _outgoing_targets(self, source: NodeId, property_key: str) -> list[NodeId]:
        return [
            e.target
            for e in self._graph.edges(source, Direction.OUT)
            if e.property_key == property_key
        ]

    # -- the connections footer (ADR 013) -----------------------------------

    def _connections(
        self,
        edges: Iterable[Edge],
        extra_names: Mapping[NodeId, str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """(label, target name, target id) rows, deterministically ordered.

        ``extra_names`` resolves targets not yet in the index (a node being
        created); unknown targets fall back to the raw id rather than fail
        a write over a cosmetic line.
        """
        names = extra_names or {}
        rows = []
        for edge in edges:
            name = names.get(edge.target) or (
                self._graph.node(edge.target).name
                if self._graph.has_node(edge.target) else edge.target
            )
            rows.append((edge.type, name, edge.target))
        rows.sort(key=lambda row: (row[0], row[1].lower(), row[2]))
        return rows

    def _footer_markdown(
        self,
        obj: Mapping[str, Any],
        node: Node,
        edges: Iterable[Edge],
        extra_names: Mapping[NodeId, str] | None = None,
    ) -> str | None:
        """The ``markdown`` value to ride an existing PATCH, or ``None``.

        ``None`` means don't touch the body: infra-role nodes never get a
        footer (write-once by policy), and an unchanged footer is a no-op
        (every skipped rewrite is a mention-pill spared, WP10c caveat).
        Composed from ``body_of`` output -- never the raw export (A8).
        """
        if node.role in schema.INFRA_ROLES:
            return None
        footer = mapping.render_connections_footer(
            self._connections(edges, extra_names), self._client.space_id
        )
        clean_body, current_footer = mapping.body_and_footer_of(obj)
        if mapping.footers_equal(footer, current_footer):
            return None
        return mapping.compose_body(clean_body, footer)

    # -- field routing (ADR 012) -------------------------------------------

    async def _resolve_field_entries(
        self, fields: Mapping[str, str]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Split ``fields`` into native property entries + the residual blob.

        A key matching a reflectable native property (by key or display
        name) writes that property -- where humans filter and sort; select
        values are resolved against the property's tags *before* the object
        write (POST validates inline select entries, so resolution cannot
        wait). Unmatched keys fall through to the ``gc_fields`` blob.
        """
        native: list[dict[str, Any]] = []
        residual: dict[str, str] = {}
        for key, value in fields.items():
            info = self._registry.field_property(key)
            if info is None:
                residual[key] = value
                continue
            native.append(await self._native_entry(info, value))
        return native, residual

    async def _native_entry(self, info: PropertyInfo, value: str) -> dict[str, Any]:
        fmt = info.format
        if fmt == "select":
            return mapping.property_entry(
                info.key, fmt, await self._resolve_tag(info, value)
            )
        if fmt == "multi_select":
            keys = [
                await self._resolve_tag(info, part.strip())
                for part in value.split(",") if part.strip()
            ]
            return mapping.property_entry(info.key, fmt, keys)
        if fmt == "number":
            try:
                return mapping.property_entry(info.key, fmt, float(value))
            except ValueError:
                raise GraphContextError(
                    f"field {info.key!r} is a number property; got {value!r} "
                    "(pass a plain number, e.g. \"42\")"
                ) from None
        if fmt == "checkbox":
            lowered = value.strip().lower()
            if lowered not in {"true", "false", "yes", "no", "1", "0"}:
                raise GraphContextError(
                    f"field {info.key!r} is a checkbox property; got {value!r} "
                    "(pass \"true\" or \"false\")"
                )
            return mapping.property_entry(
                info.key, fmt, lowered in {"true", "yes", "1"}
            )
        # text / date / url / email / phone: pass the string through.
        return mapping.property_entry(info.key, fmt, value)

    async def _send_tolerating_fresh_tags(
        self, send: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """An object write tolerant of the fresh-tag settle window.

        Mirrors :meth:`_patch_relations`: only a 400 naming an invalid
        select option while we hold unproven fresh tags is retried.
        """
        for attempt in range(_FRESH_KEY_ATTEMPTS):
            try:
                result = await send()
            except AnytypeApiError as err:
                unsettled = (
                    bool(self._unsettled_tags)
                    and err.status == 400
                    and any(m in err.detail for m in _INVALID_OPTION_MARKERS)
                )
                if unsettled and attempt < _FRESH_KEY_ATTEMPTS - 1:
                    await self._sleep(_FRESH_KEY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise
            self._unsettled_tags.clear()  # proven usable
            return result
        raise AssertionError("unreachable")  # loop always returns or raises

    async def _resolve_tag(self, info: PropertyInfo, value: str) -> str:
        """Resolve a select/multi_select value to an existing tag key,
        creating the option when missing (options are cheap; ceremony is
        not). Matches case-insensitively on tag name or key."""
        if not value:
            raise GraphContextError(
                f"field {info.key!r} is a {info.format} property and needs a "
                "non-empty option name; clearing an option is not supported "
                "yet -- set a different one or edit it in Anytype"
            )
        target = value.strip().lower()
        async for tag in self._client.list_tags(info.id):
            if target in (str(tag.get("name", "")).lower(), str(tag.get("key", "")).lower()):
                return str(tag["key"])
        created = await self._client.create_tag(
            info.id,
            # `color` is REQUIRED by CreateTagRequest (live-confirmed);
            # derived from the name so it is stable and human-recolorable.
            {"name": value.strip(), "color": _tag_color(value)},
        )
        logger.info("created tag %r on property %s", value.strip(), info.key)
        self._unsettled_tags.add(str(created["key"]))
        return str(created["key"])

    async def _rollback_create(
        self,
        node_id: NodeId,
        patched: list[tuple[NodeId, str, list[NodeId], str | None]],
    ) -> None:
        logger.warning("composite create failed; rolling back node %s", node_id)
        for source_id, property_key, previous, restore_markdown in patched:
            try:
                payload = mapping.relation_patch_payload(property_key, previous)
                if restore_markdown is not None:
                    payload["markdown"] = restore_markdown  # un-render the footer
                await self._client.update_object(source_id, payload)
            except Exception:
                # Best-effort compensation: the in-flight create error must
                # win, so a failed restore is logged (with traceback), never
                # raised over it.
                logger.exception(
                    "rollback: could not restore %s.%s", source_id, property_key
                )
        try:
            await self._client.archive_object(node_id)
        except Exception:
            logger.exception("rollback: could not archive orphan node %s", node_id)

    def _track_watermark(self, obj: Mapping[str, Any]) -> None:
        modified = mapping.effective_modified(obj)
        object_id = str(obj.get("id", ""))
        if modified and object_id:
            self._seen_stamps[object_id] = modified
        if self._watermark is not None and modified:
            self._watermark = max(self._watermark, modified)
