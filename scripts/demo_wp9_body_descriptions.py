"""WP9 demo: descriptions live in the Anytype body (ADR 010).

Shows the three behaviors the WP promises, against the mock server:

1. create_node's `description` lands in the page body and comes back
   from get_node (fetched on demand -- never hydrated into the index).
2. A HUMAN rewriting the body in the Anytype editor is visible to the
   very next get_node, with NO resync -- fresher than the old
   gc_description property ever was.
3. explore(detail="full") assembles a scene with every hit's full
   description in one call (the body fan-out).

Against a real space, step 2 is genuinely interactive: create the node,
open it in the Anytype app, edit the page body, and call get_node again.

Run:  PYTHONPATH=src python scripts/demo_wp9_body_descriptions.py
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
    for key, name in {"character": "Character", "location": "Location"}.items():
        await client.create_type(
            {"key": key, "name": name, "plural_name": f"{name}s", "layout": "basic"}
        )
    repo = AnytypeGraphRepository(client)
    await repo.hydrate()
    svc = tools.build_services(repo, SessionState(project="Ashfall"))

    def show(label: str, out: str) -> None:
        print(f"\n### {label}\n{out}")

    show("1. description lands in the body", await tools.create_node_tool(
        svc, type="Character", name="Mira", summary="Exiled siege engineer.",
        description="Born beneath the vaults of Brakk; she reads stone the "
                    "way scribes read script.",
    ))
    mira = repo.graph.resolve("Mira").id

    # A human rewrites the page body in the Anytype editor...
    mock.edit_object_directly(
        mira, markdown="Born beneath the vaults of Brakk. Since the Fall she "
                       "leads the survivors, and the stone listens back.",
    )
    show("2. human's body edit, next get_node, NO resync",
         await tools.get_node_tool(svc, node_id=mira))

    await tools.create_node_tool(
        svc, type="Location", name="The Undercroft", summary="Vaults beneath Brakk.",
        description="Cold galleries of pre-Fall stonework; every sound "
                    "arrives twice.",
        links=[{"edge_type": "located_at", "other": mira, "outgoing": False}],
        create_missing_relations=True,
    )
    show("3. explore full = scene assembly with full descriptions",
         await tools.explore_tool(svc, start=mira, depth=1, detail="full"))

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
