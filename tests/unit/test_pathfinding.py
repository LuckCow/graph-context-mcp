"""Shortest meaningful paths over the undirected view."""

import pytest

from graph_context.domain.pathfinding import find_path
from graph_context.errors import NodeNotFound
from tests.conftest import World


class TestFindPath:
    async def test_finds_shortest_path_regardless_of_edge_direction(
        self, repository, world: World
    ):
        path = find_path(repository.graph, world.ashbrand.id, world.undercroft.id)
        assert path is not None
        assert [n.name for n in path.nodes] == ["Ashbrand", "Mira", "The Undercroft"]

    async def test_edge_type_restriction_can_sever_a_path(self, repository, world: World):
        path = find_path(
            repository.graph,
            world.ashbrand.id,
            world.undercroft.id,
            edge_types=["possesses"],  # cannot continue past Mira
        )
        assert path is None

    async def test_max_length_bounds_the_search(self, repository, world: World):
        path = find_path(
            repository.graph, world.ashbrand.id, world.undercroft.id, max_length=1
        )
        assert path is None

    async def test_missing_endpoint_raises(self, repository, world: World):
        with pytest.raises(NodeNotFound):
            find_path(repository.graph, world.mira.id, "ghost")

    async def test_source_equals_target_is_a_zero_length_path(self, repository, world: World):
        path = find_path(repository.graph, world.mira.id, world.mira.id)
        assert path is not None and len(path) == 0
