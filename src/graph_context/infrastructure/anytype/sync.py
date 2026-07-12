"""Hydrate and resync: building the GraphIndex from the store.

Read philosophy: **lenient reads, strict writes.** Humans edit the space
directly in the Anytype UI, so reads must tolerate states our writers would
never produce -- edges pointing at deleted nodes, missing summaries, native
objects of any type. We skip what we cannot represent (with a warning)
instead of failing the whole hydrate; writes stay strictly validated.

``last_modified_date`` bookkeeping: we track the maximum *effective* stamp
(see :func:`mapping.effective_modified`) seen in object payloads, not
wall-clock time, so resync stays immune to clock skew. Resync issues a
``POST /search`` filtered on ``last_modified_date >= <watermark>`` across the
*whole space* (the space-reflecting model can no longer scope by a fixed set
of ``gc_`` type keys, since story nodes use the user's native types); the
modified-since filter keeps the result set to the changed slice.

Known blind spot (spike S4): archived objects do not appear in list or
search results, so human *deletions* are invisible to modified-since resync.
They are only reconciled by the next full hydrate, which rebuilds the index
from the live set.

Space members (spike S11, 2026-07-12): participant objects NEVER appear in
list or search results, but the single-object GET serves them like any
object and ``objects``-format relations accept them as targets (that is how
Anytype's own Assignee works). So both hydrate and resync fetch the space's
active members via ``/members`` + per-member GETs (members are few; this is
not an N+1 over the space) and feed the ordinary object envelopes through
``mapping.to_node`` -- members become first-class, read-only nodes the LLM
can see and link to. Member *renames* keep their modified stamp and member
*removals* mirror the S4 blind spot: both reconcile on the next full
hydrate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import NodeId
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient

if TYPE_CHECKING:
    from graph_context.infrastructure.anytype.registry import SpaceRegistry

logger = logging.getLogger(__name__)


async def load_index(
    client: AnytypeClient, registry: SpaceRegistry
) -> tuple[GraphIndex, str, dict[NodeId, str]]:
    """Full hydrate: one paged sweep + the member fetch, two in-memory
    passes (nodes, then edges).

    Returns the new index, the last-modified watermark, and the per-node
    effective-modified stamps (used by the repository for self-write
    suppression).
    """
    objects = [obj async for obj in client.list_objects()]
    swept = {obj.get("id") for obj in objects}
    objects.extend(
        obj for obj in await fetch_member_objects(client)
        if obj.get("id") not in swept  # future-proof against overlap
    )
    index = GraphIndex()
    watermark = ""
    stamps: dict[NodeId, str] = {}
    for obj in objects:
        node = mapping.to_node(obj, registry)
        if node is not None:
            stamp = mapping.effective_modified(obj)
            watermark = max(watermark, stamp)
            index.upsert_node(node)
            stamps[node.id] = stamp
    for obj in objects:
        if mapping.to_node(obj, registry) is not None:
            _apply_outgoing_edges(index, obj)
    logger.info(
        "hydrated %d nodes / %d edges from %d objects",
        index.node_count(), index.edge_count(), len(objects),
    )
    return index, watermark, stamps


async def fetch_member_objects(client: AnytypeClient) -> list[dict[str, Any]]:
    """The active members' participant objects, as ordinary envelopes (S11).

    ``/members`` is the only enumeration of participants (list/search skip
    them); the per-member GET returns a normal object payload that flows
    through :func:`mapping.to_node` like anything else. Lenient reads: a
    member whose object cannot be fetched is skipped with a warning.
    """
    objects: list[dict[str, Any]] = []
    async for member in client.list_members():
        member_id = str(member.get("id") or "")
        if not member_id or member.get("status") != "active":
            continue
        try:
            objects.append(await client.get_object(member_id))
        except GraphContextError as err:
            logger.warning("skipping member object %s: %s", member_id, err)
    return objects


async def fetch_changes(
    client: AnytypeClient, watermark: str
) -> list[dict[str, Any]]:
    """Incremental fetch via ``POST /search``: whole space, modified since ``watermark``.

    When ``watermark`` is empty (no prior watermark) the filter is omitted, so
    the search returns every object in the space.
    """
    filters = mapping.modified_since_filter(watermark) if watermark else None
    return [obj async for obj in client.search(types=None, filters=filters)]


def apply_changes(
    index: GraphIndex, changed: list[dict[str, Any]], registry: SpaceRegistry
) -> tuple[frozenset[NodeId], str]:
    """Apply out-of-band changes to the index; return (changed ids, watermark).

    Per changed object: archived -> remove; otherwise upsert the node, drop
    its previously indexed *outgoing* edges, and re-derive them from its
    relation properties. Edges pointing at ids absent from the index are
    dropped with a warning -- they may resolve on the next full hydrate.
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
        node = mapping.to_node(obj, registry)
        if node is None:
            continue
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
        index.add_edge(edge)
