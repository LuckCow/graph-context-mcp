"""Explorer: focus-stack defaults and session updates."""

import pytest

from graph_context.application.explorer import Explorer
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery
from graph_context.errors import EmptyFocusStack
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


@pytest.fixture
def explorer(repository, session) -> Explorer:
    return Explorer(repository, session)


class TestExplorerDefaults:
    async def test_empty_start_defaults_to_focus_top(
        self, explorer: Explorer, session, world: World
    ):
        session.focus.push(world.siege.id)
        result = await explorer.explore(ExploreQuery(start=""))
        assert result.hits[0].node.id == world.siege.id

    async def test_explicit_start_wins_over_focus(self, explorer: Explorer, session, world: World):
        session.focus.push(world.siege.id)
        result = await explorer.explore(ExploreQuery(start=world.ashbrand.id))
        assert result.hits[0].node.id == world.ashbrand.id

    async def test_empty_focus_raises_actionable_error(self) -> None:
        explorer = Explorer(InMemoryGraphRepository(), SessionState())
        with pytest.raises(EmptyFocusStack):
            await explorer.explore(ExploreQuery(start=""))

    async def test_explore_pushes_start_onto_focus(self, explorer, session, world: World):
        await explorer.explore(ExploreQuery(start=world.undercroft.id))
        assert session.focus.top == world.undercroft.id


class TestBodiesFor:
    """ADR 010: the explore-full fan-out lives here, not in the presenter."""

    async def test_maps_ids_to_bodies_with_empty_for_bodiless(
        self, explorer: Explorer, repository, session, world: World
    ):
        await repository.update_node(world.mira.id, body="Leads the survivors.")
        bodies = await explorer.bodies_for([world.mira.id, world.ashbrand.id])
        assert bodies == {
            world.mira.id: "Leads the survivors.",
            world.ashbrand.id: "",
        }

    async def test_no_ids_costs_no_fetches(
        self, explorer: Explorer, repository, session
    ):
        assert await explorer.bodies_for([]) == {}
