"""``AnytypeGraphRepository``: the production :class:`GraphRepository`.

Write ordering (port contract): persist to Anytype first, then mutate the
index -- the index may lag the store but never lead it. A failed API call
leaves the index untouched.

Composite-create choreography (no transactions in the API):
  1. Pre-validate every link against the index (endpoint existence +
     schema rules) -- cheap, and prevents most orphan writes outright.
  2. POST the node with its *outgoing* relations inline (zero extra calls).
  3. For *incoming* links, PATCH each source object's relation property
     (read-modify-write from index state, since PATCH replaces lists --
     mapping assumption A4).
  4. On any failure after the POST: archive the created node and restore
     every already-patched source to its previous target list, then
     re-raise. The store ends exactly where it started.

Concurrency stance (settled, WP1): last-write-wins versus human edits.
The read-modify-write in step 3 reads from the *index*; a human edit to
the same relation property between our last sync and this write will be
overwritten. v1 accepts this and logs loudly at the sync layer instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from graph_context.domain import schema
from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Edge, LinkSpec, Node, NodeDraft, NodeId
from graph_context.domain.schema import EdgeType
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype import mapping, sync
from graph_context.infrastructure.anytype.client import AnytypeClient

logger = logging.getLogger(__name__)


class AnytypeGraphRepository:
    """Write-through repository over the Anytype local API."""

    def __init__(self, client: AnytypeClient) -> None:
        self._client = client
        self._graph = GraphIndex()
        self._watermark: str | None = None  # None until first hydrate
        # Self-write suppression: last_modified stamps already accounted
        # for (our own writes + hydrate), so resync's [gte] watermark query
        # never reports our own boundary write as an out-of-band change.
        self._seen_stamps: dict[NodeId, str] = {}

    @property
    def graph(self) -> GraphIndex:
        return self._graph

    # -- sync -------------------------------------------------------------

    async def hydrate(self) -> None:
        self._graph, watermark, stamps = await sync.load_index(self._client)
        self._watermark = watermark
        self._seen_stamps = stamps

    async def resync(self) -> frozenset[NodeId]:
        """Apply out-of-band changes; first call without hydrate = full load."""
        if self._watermark is None:
            await self.hydrate()
            return frozenset(node.id for node in self._graph.nodes())
        fetched = await sync.fetch_changes(self._client, self._watermark)
        unseen = [
            obj for obj in fetched
            if mapping.effective_modified(obj)
            > self._seen_stamps.get(obj.get("id", ""), "")
        ]
        changed_ids, watermark = sync.apply_changes(self._graph, unseen)
        for obj in unseen:
            self._seen_stamps[obj["id"]] = mapping.effective_modified(obj)
        self._watermark = max(self._watermark, watermark)
        if changed_ids:
            logger.info("resync applied %d out-of-band changes", len(changed_ids))
        return changed_ids

    async def fetch_body(self, node_id: NodeId) -> str:
        """On-demand body read (A5). The only code path that fetches a
        single object -- never called during hydrate, by design."""
        self._graph.node(node_id)  # NodeNotFound before spending an API call
        obj = await self._client.get_object(node_id)
        return str(obj.get("markdown", "") or "")

    # -- writes -----------------------------------------------------------

    async def create_node(
        self, draft: NodeDraft, links: Sequence[LinkSpec] = ()
    ) -> Node:
        self._prevalidate_links(draft, links)
        outgoing: dict[EdgeType, list[NodeId]] = {}
        incoming = []
        for link in links:
            if link.outgoing:
                outgoing.setdefault(link.edge_type, []).append(link.other)
            else:
                incoming.append(link)

        created = await self._client.create_object(
            mapping.to_create_payload(draft, outgoing)
        )
        node = mapping.to_node(created)
        if node is None:  # defensive: the store returned something unusable
            raise GraphContextError(
                f"created object {created.get('id')} did not map back to a node"
            )

        patched: list[tuple[NodeId, EdgeType, list[NodeId]]] = []
        try:
            for link in incoming:
                previous = self._outgoing_targets(link.other, link.edge_type)
                patched_source = await self._client.update_object(
                    link.other,
                    mapping.relation_patch_payload(
                        link.edge_type, [*previous, node.id]
                    ),
                )
                patched.append((link.other, link.edge_type, previous))
                self._track_watermark(patched_source)
        except Exception:
            await self._rollback_create(node.id, patched)
            raise

        # Persisted everywhere -- now (and only now) mutate the index.
        self._graph.upsert_node(node)
        for link in links:
            self._graph.add_edge(link.to_edge(anchor=node.id))
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
        updated = await self._client.update_object(node_id, payload)
        node = mapping.to_node(updated)
        if node is None:
            raise GraphContextError(f"updated object {node_id} did not map back")
        self._graph.upsert_node(node)
        self._track_watermark(updated)
        return node

    async def add_link(self, anchor: NodeId, link: LinkSpec) -> Edge:
        edge = link.to_edge(anchor=anchor)
        self._validate_edge(edge)
        targets = self._outgoing_targets(edge.source, edge.type)
        if edge.target not in targets:
            updated = await self._client.update_object(
                edge.source,
                mapping.relation_patch_payload(edge.type, [*targets, edge.target]),
            )
            self._track_watermark(updated)
        self._graph.add_edge(edge)
        return edge

    async def remove_link(self, edge: Edge) -> None:
        targets = [
            t for t in self._outgoing_targets(edge.source, edge.type) if t != edge.target
        ]
        updated = await self._client.update_object(
            edge.source, mapping.relation_patch_payload(edge.type, targets)
        )
        self._graph.remove_edge(edge)
        self._track_watermark(updated)

    # -- internals ----------------------------------------------------------

    def _prevalidate_links(self, draft: NodeDraft, links: Sequence[LinkSpec]) -> None:
        """Catch bad links before anything is persisted (cheap, index-only)."""
        for link in links:
            other = self._graph.node(link.other)  # raises NodeNotFound
            if link.outgoing:
                schema.validate_edge(draft.type, link.edge_type, other.type)
            else:
                schema.validate_edge(other.type, link.edge_type, draft.type)

    def _validate_edge(self, edge: Edge) -> None:
        source = self._graph.node(edge.source)
        target = self._graph.node(edge.target)
        schema.validate_edge(source.type, edge.type, target.type)

    def _outgoing_targets(self, source: NodeId, edge_type: EdgeType) -> list[NodeId]:
        return [
            e.target
            for e in self._graph.edges(source, Direction.OUT, edge_types=[edge_type])
        ]

    async def _rollback_create(
        self,
        node_id: NodeId,
        patched: list[tuple[NodeId, EdgeType, list[NodeId]]],
    ) -> None:
        logger.warning("composite create failed; rolling back node %s", node_id)
        for source_id, edge_type, previous in patched:
            try:
                await self._client.update_object(
                    source_id, mapping.relation_patch_payload(edge_type, previous)
                )
            except Exception:  # noqa: BLE001 -- best-effort compensation
                logger.error("rollback: could not restore %s.%s", source_id, edge_type)
        try:
            await self._client.archive_object(node_id)
        except Exception:  # noqa: BLE001
            logger.error("rollback: could not archive orphan node %s", node_id)

    def _track_watermark(self, obj: Mapping[str, Any]) -> None:
        # Spike S3: our own create/PATCH responses carry the timestamps as
        # date properties (a fresh create has only created_date; a PATCH adds
        # last_modified_date), so the effective stamp covers both.
        modified = mapping.effective_modified(obj)
        object_id = str(obj.get("id", ""))
        if modified and object_id:
            self._seen_stamps[object_id] = modified
        if self._watermark is not None and modified:
            self._watermark = max(self._watermark, modified)
