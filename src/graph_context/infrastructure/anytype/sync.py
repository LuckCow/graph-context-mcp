"""Hydrate and resync: building the GraphIndex from the store.

Read philosophy: **lenient reads, strict writes.** Humans edit the space
directly in the Anytype UI, so reads must tolerate states our writers
would never produce -- edges pointing at deleted nodes, schema-illegal
edges dragged together by hand, missing summaries. We skip what we cannot
represent (with a warning) instead of failing the whole hydrate; writes,
by contrast, stay strictly validated.

``last_modified_date`` bookkeeping: we track the maximum *effective* stamp
(``last_modified_date`` if surfaced, else ``created_date``; see
:func:`mapping.effective_modified`) seen in object payloads, not wall-clock
time, so resync stays immune to clock skew between this process and
anytype-heart. Resync issues a ``POST /search`` filtered on
``last_modified_date >= <watermark>`` (spike S3: ``GET /objects`` takes no
filters; only search does, and it pages at 100); re-processing the boundary
second is harmless because applying changes is idempotent.

Known blind spot (spike S4, WP1 Q3 -- now *confirmed*): archived objects do
not appear in list or search results and cannot be enumerated, so human
*deletions* are invisible to modified-since resync. They are only reconciled
by the next full hydrate, which rebuilds the index from the live set. The
archived branch in ``apply_changes`` is therefore unreachable via live
search and exists only as defensive cover (and for the mock's tests).
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


async def load_index(client: AnytypeClient) -> tuple[GraphIndex, str, dict[NodeId, str]]:
    """Full hydrate: one paged sweep, two in-memory passes (nodes, then edges).

    Returns the new index, the last-modified watermark, and the per-node
    effective-modified stamps (used by the repository for self-write
    suppression -- see :meth:`AnytypeGraphRepository.resync`).
    """
    objects = [obj async for obj in client.list_objects()]
    index = GraphIndex()
    watermark = ""
    stamps: dict[NodeId, str] = {}
    for obj in objects:
        node = mapping.to_node(obj)
        if node is not None:
            stamp = mapping.effective_modified(obj)
            watermark = max(watermark, stamp)
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
    """Incremental fetch via ``POST /search``: our types, modified since ``watermark``.

    When ``watermark`` is empty (no prior sync watermark) the filter is
    omitted, so the search returns every object of our types.
    """
    filters = mapping.modified_since_filter(watermark) if watermark else None
    return [
        obj
        async for obj in client.search(types=mapping.ALL_TYPE_KEYS, filters=filters)
    ]


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
        watermark = max(watermark, mapping.effective_modified(obj))
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
