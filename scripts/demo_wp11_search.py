"""WP11 acceptance demo: describe a node, and the graph explains the answer.

Runs with GC_EMBEDDER=hash (deterministic, model-free) against the memory
backend through the shared builder -- the exact stack CI uses. Shows the
three surfaces: find_node's semantic tier with evidence, the resolver's
"closest by meaning" suggestions on every tool, and the ADR 016
recruitment case (an item whose text shares nothing with the query).

Run:  GC_EMBEDDER=hash PYTHONPATH=src python scripts/demo_wp11_search.py
"""

import asyncio
import os

from graph_context import composition
from graph_context.interface import tools
from graph_context.interface.profiles import get_profile


async def main() -> None:
    os.environ["GC_BACKEND"] = "memory"
    os.environ.setdefault("GC_EMBEDDER", "hash")
    profile = get_profile("fiction")
    services, teardown = await composition.build_runtime(profile)

    async def show(label: str, out: str) -> None:
        print(f"\n### {label}\n{out.split(chr(10), 1)[1]}")

    # Build a small world through the tools (the projector tracks writes
    # via resync in production; here we refresh after the burst).
    await tools.create_node_tool(
        services, type="Character", name="Mira",
        summary="Exiled siege engineer of Brakk.",
    )
    await tools.create_node_tool(
        services, type="Event", name="Siege of Brakk", story_time=10,
        summary="The year-long siege in which the city fell.",
        links=[{"edge_type": "participated_in", "other": "Mira",
                "outgoing": False}],
    )
    await tools.create_node_tool(
        services, type="Item", name="Ashbrand",
        summary="A blade quenched in ash.",  # zero 'siege' vocabulary
        links=[
            {"edge_type": "wielded_by", "other": "Mira"},
            {"edge_type": "used_in", "other": "Siege of Brakk"},
        ],
        create_missing_relations=True,
    )
    assert services.projector is not None
    await services.projector.refresh()

    await show("describe, don't name (find_node tier 3)",
               await tools.find_node_tool(services, name="the exiled engineer"))

    await show("recruitment: the item's text never says 'siege'",
               await tools.find_node_tool(
                   services, name="the blade wielded in the siege"))

    await show("every tool suggests on a miss (resolver, ADR 016)",
               await tools.get_node_tool(
                   services, node_id="the engineer who broke the walls"))

    await composition.run_teardown(teardown)


if __name__ == "__main__":
    asyncio.run(main())
