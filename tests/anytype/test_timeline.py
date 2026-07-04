"""The profile-declared time axis (ADR 015): one ordered value, any source."""

from __future__ import annotations

import pytest

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, Node, NodeDraft
from graph_context.domain.schema import Role
from graph_context.domain.traversal import ExploreQuery, explore
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
from tests.anytype.conftest import seed_native_types


async def _date_axis_repo(mock: MockAnytype) -> tuple[AnytypeGraphRepository, AnytypeClient]:
    config = AnytypeConfig(api_key="test", space_id=mock.space_id, page_limit=10)
    client = AnytypeClient(config, transport=mock.transport)
    await ensure_schema(client, timeline=("event_date", "date"))
    await seed_native_types(client)
    repo = AnytypeGraphRepository(client, timeline=("event_date", "date"))
    await repo.hydrate()
    return repo, client


async def test_date_axis_round_trips_and_bootstraps_the_property(
    mock: MockAnytype,
) -> None:
    repo, client = await _date_axis_repo(mock)
    node = await repo.create_node(NodeDraft(
        "Event", name="Standup", summary="Daily.", story_time="2026-07-04",
    ))
    assert node.story_time == "2026-07-04"
    stored = {p["key"]: p for p in mock.object(node.id)["properties"]}
    assert stored["event_date"]["format"] == "date"
    assert stored["event_date"]["date"] == "2026-07-04"
    assert "gc_story_time" not in stored
    updated = await repo.update_node(node.id, story_time="2026-07-05")
    assert updated.story_time == "2026-07-05"
    await client.aclose()


async def test_timeline_property_never_reflects_into_fields(
    mock: MockAnytype,
) -> None:
    """event_date is surfaced as story_time; doubling it in fields would be
    noise (ADR 015 + the ADR 012 filter working together)."""
    repo, client = await _date_axis_repo(mock)
    node = await repo.create_node(NodeDraft(
        "Event", name="Standup", summary="Daily.", story_time="2026-07-04",
    ))
    assert "event_date" not in repo.graph.node(node.id).fields
    await client.aclose()


def _event(node_id: str, when: float | str) -> Node:
    return Node(id=node_id, type="Event", name=node_id, summary="s",
                story_time=when, role=Role.EVENT)


def _dated_world() -> GraphIndex:
    g = GraphIndex()
    g.upsert_node(Node(id="mira", type="Character", name="Mira", summary="s",
                       role=Role.CHARACTER))
    g.upsert_node(_event("past", "2026-06-01"))
    g.upsert_node(_event("future", "2026-08-01"))
    g.add_edge(Edge("mira", "participated_in", "past"))
    g.add_edge(Edge("mira", "participated_in", "future"))
    return g


def test_as_of_orders_iso_dates() -> None:
    g = _dated_world()
    hits = explore(g, ExploreQuery(start="mira", as_of="2026-07-04"))
    assert {h.node.id for h in hits.hits} == {"mira", "past"}  # future hidden
    hits = explore(g, ExploreQuery(start="mira", as_of="2026-07-04",
                                   include_future=True))
    assert {h.node.id for h in hits.hits} == {"mira", "past", "future"}


def test_mixed_timeline_types_error_actionably() -> None:
    g = _dated_world()
    with pytest.raises(GraphContextError, match="not comparable"):
        explore(g, ExploreQuery(start="mira", as_of=10))
