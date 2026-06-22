"""Hydrate and resync: building the GraphIndex from the store.

Read philosophy: **lenient reads, strict writes.** Humans edit the space
directly in the Anytype UI, so reads must tolerate states our writers
would never produce -- edges pointing at deleted nodes, schema-illegal
edges dragged together by hand, missing summaries. We skip what we cannot
represent (with a warning) instead of failing the whole hydrate; writes,
by contrast, stay strictly validated.

``last_modified_date`` bookkeeping: we track the maximum value *seen in
object payloads* rather than wall-clock time, so resync is immune to
clock skew between this process and anytype-heart. Resync queries
``last_modified_date[gte]=<watermark>``; re-processing the boundary object
is harmless because applying changes is idempotent.

Known blind spot (spike S4, WP1 Q3): if archived objects do not appear in
modified-since listings, human *deletions* are invisible to resync and are
only reconciled by the next full hydrate. ``apply_changes`` handles
archived objects when they *are* visible, so either spike answer works.
"""

from __future__ import annotations

import logging
from typing import Any

from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import NodeId
from graph_context.errors import SchemaViolation
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient

logger = logging.getLogger(__name__)

MODIFIED_SINCE_PARAM = "last_modified_date[gte]"


async def load_index(client: AnytypeClient) -> tuple[GraphIndex, str, dict[NodeId, str]]:
    """Full hydrate: one paged sweep, two in-memory passes (nodes, then edges).

    Returns the new index, the last-modified watermark, and the per-node
    last-modified stamps (used by the repository for self-write
    suppression -- see :meth:`AnytypeGraphRepository.resync`).
    """
    objects = [obj async for obj in client.list_objects()]
    index = GraphIndex()
    watermark = ""
    stamps: dict[NodeId, str] = {}
    for obj in objects:
        stamp = obj.get("last_modified_date", "")
        watermark = max(watermark, stamp)
        node = mapping.to_node(obj)
        if node is not None:
            index.upsert_node(node)
            stamps[node.id] = stamp
    for obj in objects:
        if mapping.to_node(obj) is not None:
            _apply_outgoing_edges(index, obj)
    logger.info(
        "hydrated %d nodes / %d edges from %d objects",
        index.node_count(), index.edge_count(), len(objects),
    )
    return index, watermark, stamps


async def fetch_changes(
    client: AnytypeClient, watermark: str
) -> list[dict[str, Any]]:
    """Incremental fetch: typically a single filtered call."""
    params = {MODIFIED_SINCE_PARAM: watermark} if watermark else {}
    return [obj async for obj in client.list_objects(**params)]


def apply_changes(
    index: GraphIndex, changed: list[dict[str, Any]]
) -> tuple[frozenset[NodeId], str]:
    """Apply out-of-band changes to the index; return (changed ids, watermark).

    Per changed object: archived -> remove; otherwise upsert the node,
    drop its previously indexed *outgoing* edges, and re-derive them from
    its relation properties (incoming edges live on other objects and are
    untouched). Edges pointing at ids absent from the index are dropped
    with a warning -- they may resolve on the next full hydrate.
    """
    changed_ids: set[NodeId] = set()
    watermark = ""
    for obj in changed:
        watermark = max(watermark, obj.get("last_modified_date", ""))
        object_id = obj.get("id", "")
        if obj.get("archived"):
            if index.has_node(object_id):
                index.remove_node(object_id)
                changed_ids.add(object_id)
            continue
        node = mapping.to_node(obj)
        if node is None:
            continue  # not a gc_ object; a human edited something unrelated
        index.upsert_node(node)
        for edge in list(index.edges(node.id, Direction.OUT)):
            index.remove_edge(edge)
        _apply_outgoing_edges(index, obj)
        changed_ids.add(node.id)
    return frozenset(changed_ids), watermark


def _apply_outgoing_edges(index: GraphIndex, obj: dict[str, Any]) -> None:
    for edge in mapping.to_edges(obj):
        if not index.has_node(edge.target):
            logger.warning("skipping dangling edge %s (target missing)", edge)
            continue
        try:
            index.add_edge(edge)
        except SchemaViolation as violation:
            logger.warning("skipping schema-illegal edge %s: %s", edge, violation)
