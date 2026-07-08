"""Explorer: session-default starts and touch behaviour."""

import pytest

from graph_context.application.explorer import Explorer
from graph_context.domain.session import SessionState
from graph_context.domain.traversal import ExploreQuery
from graph_context.errors import NoDefaultStart
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from tests.conftest import World


@pytest.fixture
def explorer(repository, session) -> Explorer:
    return Explorer(repository, session)


class TestExplorerDefaults:
    async def test_empty_start_defaults_to_the_held_node(
        self, explorer: Explorer, session, world: World
    ):
        session.working_set.hold(world.siege.id)
        result = await explorer.explore(ExploreQuery(start=""))
        assert result.hits[0].node.id == world.siege.id

    async def test_empty_start_falls_back_to_most_recently_touched(
        self, explorer: Explorer, session, world: World
    ):
        session.touch(world.undercroft.id)
        result = await explorer.explore(ExploreQuery(start=""))
        assert result.hits[0].node.id == world.undercroft.id

    async def test_explicit_start_wins_over_held(
        self, explorer: Explorer, session, world: World
    ):
        session.working_set.hold(world.siege.id)
        result = await explorer.explore(ExploreQuery(start=world.ashbrand.id))
        assert result.hits[0].node.id == world.ashbrand.id

    async def test_fresh_session_raises_actionable_error(self) -> None:
        explorer = Explorer(InMemoryGraphRepository(), SessionState())
        with pytest.raises(NoDefaultStart):
            await explorer.explore(ExploreQuery(start=""))

    async def test_explore_touches_start_but_never_holds_it(
        self, explorer, session, world: World
    ):
        await explorer.explore(ExploreQuery(start=world.undercroft.id))
        assert session.recent.items[0] == world.undercroft.id
        assert session.working_set.entries == ()


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
