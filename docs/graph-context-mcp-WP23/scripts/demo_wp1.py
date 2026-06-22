"""WP1 acceptance demo, runnable without a live Anytype (uses MockAnytype).

Walks the acceptance scenario from the work-package spec:
  1. bootstrap an empty space (types + properties)
  2. build a small world through the repository (composite writes)
  3. "restart": fresh repository, hydrate, prove graph equality
  4. a "human" edits the space out-of-band; resync reports exactly that
  5. show the call budget for hydrate (one paged sweep, no N+1)

Run:  python scripts/demo_wp1.py
"""

import asyncio

from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema


def snapshot(graph):
    nodes = tuple(sorted((n.id, n.name) for n in graph.nodes()))
    edges = tuple(sorted(
        (e.source, e.type.value, e.target)
        for n in graph.nodes() for e in graph.edges(n.id)
    ))
    return nodes, edges


async def main() -> None:
    mock = MockAnytype()
    config = AnytypeConfig(api_key="demo", space_id=mock.space_id)
    client = AnytypeClient(config, transport=mock.transport)

    print("== 1. bootstrap ==")
    await ensure_schema(client)
    print(f"   schema ensured ({client.request_count} API calls)\n")

    print("== 2. build world through the repository ==")
    repo = AnytypeGraphRepository(client)
    await repo.hydrate()
    mira = await repo.create_node(
        NodeDraft(NodeType.CHARACTER, "Mira", "Exiled siege engineer.")
    )
    undercroft = await repo.create_node(
        NodeDraft(NodeType.LOCATION, "The Undercroft", "Vaults beneath Brakk."),
        links=[LinkSpec(EdgeType.LOCATED_AT, other=mira.id, outgoing=False)],
    )
    await repo.create_node(
        NodeDraft(NodeType.EVENT, "Siege of Brakk", "The city falls.", story_time=10),
        links=[
            LinkSpec(EdgeType.PARTICIPATED_IN, other=mira.id, outgoing=False),
            LinkSpec(EdgeType.LOCATED_AT, other=undercroft.id),
        ],
    )
    print(f"   {repo.graph.node_count()} nodes / {repo.graph.edge_count()} edges\n")

    print("== 3. restart + hydrate ==")
    fresh_client = AnytypeClient(config, transport=mock.transport)
    fresh = AnytypeGraphRepository(fresh_client)
    before = fresh_client.request_count
    await fresh.hydrate()
    calls = fresh_client.request_count - before
    same = snapshot(fresh.graph) == snapshot(repo.graph)
    print(f"   hydrate used {calls} API call(s); graphs identical: {same}\n")

    print("== 4. human edits the space in the Anytype UI ==")
    mock.edit_object_directly(mira.id, name="Mira of Brakk")
    orla = mock.seed_object("gc_character", "Orla", properties=[
        mapping.property_entry("gc_summary", "text", "A smuggler Mira trusts."),
        mapping.property_entry("gc_edge_knows", "objects", [mira.id]),
    ])
    changed = await fresh.resync()
    names = sorted(fresh.graph.node(i).name for i in changed)
    print(f"   resync reports {len(changed)} changed node(s): {names}")
    print(f"   Orla -> knows -> {fresh.graph.node(mira.id).name}: "
          f"{any(n.id == mira.id for _, n in fresh.graph.neighbors(orla))}\n")

    print("== 5. our own writes are never reported ==")
    await fresh.update_node(mira.id, summary="Engineer turned survivor-leader.",
                            summary_stale=False)
    print(f"   resync after own write: {sorted(await fresh.resync()) or 'no changes'}")

    await client.aclose()
    await fresh_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
