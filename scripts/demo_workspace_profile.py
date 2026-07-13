"""WP5 smoke demo: the same tool surface as a WORK knowledge base.

Drives the tool functions in-process against the MockAnytype-backed
repository with the ``workspace`` profile's role overrides: people, teams,
projects, meetings, decisions. Proves the profile is framing-only -- the
code paths are identical to the fiction demo -- and that the ``meeting`` /
``decision`` -> Event role override gives real-world time the same
timeline semantics (`story_time` invariant, `as_of` filtering) that story
events get.

Run:  PYTHONPATH=src python scripts/demo_workspace_profile.py
"""

import asyncio

from graph_context.domain.session import SessionState
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from graph_context.interface import tools
from graph_context.interface.profiles import get_profile
from graph_context.interface.services import build_services


async def main() -> None:
    profile = get_profile("workspace")
    mock = MockAnytype()
    config = AnytypeConfig(api_key="demo", space_id=mock.space_id)
    client = AnytypeClient(config, transport=mock.transport)
    await ensure_schema(client)
    # Space-reflecting model: seed the user's native types (this demo space
    # is empty). In a real space these already exist.
    for key, name in {"person": "Person", "team": "Team", "project": "Project",
                      "meeting": "Meeting", "decision": "Decision"}.items():
        await client.create_type(
            {"key": key, "name": name, "plural_name": f"{name}s", "layout": "basic"}
        )
    repo = AnytypeGraphRepository(client, role_overrides=profile.role_overrides)
    await repo.hydrate()
    svc = build_services(repo, SessionState(project="Platform Team"))

    def show(label, out):
        print(f"\n### {label}\n{out}")

    show("create alice", await tools.create_node_tool(
        svc, type="Person", name="Alice Reyes", summary="Staff engineer, storage."))
    alice = svc.session.recent.items[0]
    show("create team (linked in one call)", await tools.create_node_tool(
        svc, type="Team", name="Storage Guild", summary="Owns the storage layer.",
        links=[{"edge_type": "member_of", "other": alice, "outgoing": False}],
        create_missing_relations=True))

    # The role override at work: a Meeting is an Event-role node, so it
    # requires a timeline position -- the error is the proof.
    show("meeting without a time is rejected (Event role)",
         await tools.create_node_tool(
             svc, type="Meeting", name="Q3 replatform sync",
             summary="Decide the Q3 storage replatform approach."))

    show("meeting with attendees (one call)", await tools.create_node_tool(
        svc, type="Meeting", name="Q3 replatform sync",
        summary="Decide the Q3 storage replatform approach.",
        story_time=20260630,
        links=[{"edge_type": "attended", "other": alice, "outgoing": False}],
        create_missing_relations=True))
    meeting = svc.session.recent.items[0]

    show("decision recorded against the meeting", await tools.create_node_tool(
        svc, type="Decision", name="Adopt object storage",
        summary="Move blob data to object storage in Q3.", story_time=20260630,
        links=[{"edge_type": "decided_in", "other": meeting}],
        create_missing_relations=True))
    decision = svc.session.recent.items[0]

    show("meeting brief via explore", await tools.explore_tool(
        svc, start=meeting, depth=2, detail="summaries"))

    show("how is alice related to the decision?", await tools.find_path_tool(
        svc, target=decision, start=alice))

    # Capture is the orchestrator's job now (WP7/WP12): the CaptureRecorder
    # service stands in here for what an authoring-mode turn does itself.
    await svc.capture.record(
        text="Attendees agreed to move blob data to object storage in Q3. "
             "Alice to draft the migration plan.",
        summary="Q3 replatform sync: object storage adopted.",
        references=[meeting, decision, alice],
    )

    show("overview (cold-start map)", await tools.context_tool(svc, action="overview"))

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
