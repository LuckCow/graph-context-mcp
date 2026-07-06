"""WP2/WP3 smoke demo: drive the tool functions in-process (no MCP client).

Exercises the full loop a real session would: create, explore with scene-
assembly parameters, find_path, stale-summary workflow, resync
reporting -- against the MockAnytype-backed repository, so this is also
an end-to-end pass through every layer. (Prose capture is the
orchestrator's job now -- see demo_wp7_provenance.py.)

Run:  PYTHONPATH=src python scripts/demo_wp2_tools.py
"""

import asyncio

from graph_context.domain.session import SessionState
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from graph_context.interface import tools


async def main() -> None:
    mock = MockAnytype()
    config = AnytypeConfig(api_key="demo", space_id=mock.space_id)
    client = AnytypeClient(config, transport=mock.transport)
    await ensure_schema(client)
    # Space-reflecting model: seed the user's native types (this demo space is
    # empty). In a real space these already exist.
    for key, name in {"character": "Character", "event": "Event",
                      "location": "Location", "item": "Item"}.items():
        await client.create_type(
            {"key": key, "name": name, "plural_name": f"{name}s", "layout": "basic"}
        )
    repo = AnytypeGraphRepository(client)
    await repo.hydrate()
    svc = tools.build_services(repo, SessionState(project="Ashfall"))

    def show(label, out):
        print(f"\n### {label}\n{out}")

    show("create mira", await tools.create_node_tool(
        svc, type="Character", name="Mira", summary="Exiled siege engineer."))
    mira = svc.session.focus.top
    show("create siege (linked in one call)", await tools.create_node_tool(
        svc, type="Event", name="Siege of Brakk", summary="The city falls.",
        story_time=10,
        links=[{"edge_type": "participated_in", "other": mira, "outgoing": False}]))
    siege = svc.session.focus.top

    show("scene assembly via explore", await tools.explore_tool(
        svc, start=siege, depth=2, include_types=["Character", "Location", "Item"],
        detail="summaries", as_of=10))

    show("find_path", await tools.find_path_tool(svc, target=mira, start=siege))

    show("update without summary -> stale", await tools.update_node_tool(
        svc, node_id=mira, description="Now leads the survivors."))
    show("stale sweep", await tools.explore_tool(
        svc, start=mira, only_stale=True, detail="names"))

    mock.edit_object_directly(mira, name="Mira of Brakk")
    show("resync after human edit", await tools.context_tool(svc, action="resync"))

    show("bad input -> actionable error", await tools.create_node_tool(
        svc, type="Charcter", name="Typo", summary="x"))

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
