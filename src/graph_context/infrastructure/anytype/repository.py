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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from graph_context.domain import schema
from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Edge, LinkSpec, Node, NodeDraft, NodeId
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


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "relation"


class AnytypeGraphRepository:
    """Write-through repository over the Anytype local API."""

    def __init__(
        self,
        client: AnytypeClient,
        *,
        role_overrides: Mapping[str, Role] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._graph = GraphIndex()
        # Profile-supplied type-key -> Role additions (WP5); merged into the
        # registry on every (re)load so resync keeps them.
        self._role_overrides: dict[str, Role] = dict(role_overrides or {})
        self._registry = SpaceRegistry(role_overrides=dict(self._role_overrides))
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
        self._watermark: str | None = None  # None until first hydrate
        # Self-write suppression: stamps already accounted for (our own writes
        # + hydrate), so resync's watermark query never reports our own
        # boundary write as an out-of-band change.
        self._seen_stamps: dict[NodeId, str] = {}

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
            self._client, extra_role_overrides=self._role_overrides
        )
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
            self._client, extra_role_overrides=self._role_overrides
        )
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
        """On-demand body read (A5). The only path that fetches one object."""
        self._graph.node(node_id)  # NodeNotFound before spending an API call
        obj = await self._client.get_object(node_id)
        return str(obj.get("markdown", "") or "")

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

            created = await self._client.create_object(
                mapping.to_create_payload(draft, type_key=type_key)
            )
            node = mapping.to_node(created, self._registry)
            if node is None:  # defensive: the store returned something unusable
                raise GraphContextError(
                    f"created object {created.get('id')} did not map back to a node"
                )

            patched: list[tuple[NodeId, str, list[NodeId]]] = []
            try:
                # Outgoing relations are PATCHed onto the new object rather than
                # inlined in the POST: a freshly-created relation is not yet on the
                # type, so an inline POST would 400. A failure here falls through to
                # the rollback, which archives the node (and with it these edges).
                if outgoing:
                    patched_self = await self._patch_relations(
                        node.id, mapping.relations_patch_payload(outgoing), outgoing
                    )
                    self._track_watermark(patched_self)
                for link, key in incoming:
                    # Store-truth read (ADR 009): also makes the rollback
                    # restore what the store really held, not an index view.
                    previous = await self._current_targets(link.other, key)
                    patched_source = await self._patch_relations(
                        link.other,
                        mapping.relation_patch_payload(key, [*previous, node.id]),
                        [key],
                    )
                    patched.append((link.other, key, previous))
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
        description: str | None = None,
        story_time: float | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Node:
        self._graph.node(node_id)  # NodeNotFound before any API call
        payload = mapping.to_update_payload(
            name=name,
            summary=summary,
            summary_stale=summary_stale,
            description=description,
            story_time=story_time,
            fields=fields,
        )
        async with self._writer():
            updated = await self._client.update_object(node_id, payload)
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
            self._graph.node(edge.source)  # endpoints must exist
            self._graph.node(edge.target)
            targets = await self._current_targets(edge.source, key)
            if edge.target not in targets:
                updated = await self._patch_relations(
                    edge.source,
                    mapping.relation_patch_payload(key, [*targets, edge.target]),
                    [key],
                )
                self._track_watermark(updated)
            self._graph.add_edge(edge)
            return edge

    async def remove_link(self, edge: Edge) -> None:
        key = edge.property_key or self._registry.key_for_label(edge.type)
        if key is None:  # nothing on the store to patch; drop from the index
            self._graph.remove_edge(edge)
            return
        async with self._writer():
            targets = [
                t for t in await self._current_targets(edge.source, key)
                if t != edge.target
            ]
            updated = await self._client.update_object(
                edge.source, mapping.relation_patch_payload(key, targets)
            )
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

    async def _current_targets(self, source: NodeId, property_key: str) -> list[NodeId]:
        """Store-truth targets of one relation, read at write time.

        Reads are unthrottled (S7), so the extra GET is cheap. This is also
        the precise Q2 race detector: divergence from the index view means a
        human edited this relation since the last (re)sync. The write then
        builds on store truth, so the human's edit SURVIVES instead of being
        clobbered by an index-derived wholesale-replace PATCH.
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

    async def _rollback_create(
        self,
        node_id: NodeId,
        patched: list[tuple[NodeId, str, list[NodeId]]],
    ) -> None:
        logger.warning("composite create failed; rolling back node %s", node_id)
        for source_id, property_key, previous in patched:
            try:
                await self._client.update_object(
                    source_id, mapping.relation_patch_payload(property_key, previous)
                )
            except Exception:  # noqa: BLE001 -- best-effort compensation
                logger.error("rollback: could not restore %s.%s", source_id, property_key)
        try:
            await self._client.archive_object(node_id)
        except Exception:  # noqa: BLE001
            logger.error("rollback: could not archive orphan node %s", node_id)

    def _track_watermark(self, obj: Mapping[str, Any]) -> None:
        modified = mapping.effective_modified(obj)
        object_id = str(obj.get("id", ""))
        if modified and object_id:
            self._seen_stamps[object_id] = modified
        if self._watermark is not None and modified:
            self._watermark = max(self._watermark, modified)
