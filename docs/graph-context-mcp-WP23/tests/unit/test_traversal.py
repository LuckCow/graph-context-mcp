"""Bounded BFS semantics: depth, filters, as_of, truncation."""

from graph_context.domain.schema import EdgeType, NodeType
from graph_context.domain.traversal import ExploreQuery, explore
from tests.conftest import World


class TestExplore:
    async def test_depth_one_returns_direct_neighbors_only(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id, depth=1))
        names = {hit.node.name for hit in result.hits}
        assert names == {"Mira", "Siege of Brakk", "Fall of Brakk", "The Undercroft", "Ashbrand"}
        assert all(hit.depth <= 1 for hit in result.hits)

    async def test_start_is_depth_zero_with_no_via_edge(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id))
        start_hit = result.hits[0]
        assert start_hit.node.id == world.mira.id
        assert start_hit.depth == 0 and start_hit.via is None

    async def test_as_of_hides_future_events(self, repository, world: World):
        result = explore(
            repository.graph, ExploreQuery(start=world.mira.id, depth=1, as_of=50)
        )
        names = {hit.node.name for hit in result.hits}
        assert "Siege of Brakk" in names
        assert "Fall of Brakk" not in names

    async def test_include_future_restores_future_events(self, repository, world: World):
        result = explore(
            repository.graph,
            ExploreQuery(start=world.mira.id, depth=1, as_of=50, include_future=True),
        )
        assert "Fall of Brakk" in {hit.node.name for hit in result.hits}

    async def test_node_type_filters_prune_subtrees(self, repository, world: World):
        # The Undercroft is only reachable from the siege at depth 2 via Mira
        # or the siege itself; excluding Characters must not leak paths through Mira.
        result = explore(
            repository.graph,
            ExploreQuery(
                start=world.siege.id,
                depth=2,
                exclude_node_types=frozenset({NodeType.CHARACTER}),
            ),
        )
        names = {hit.node.name for hit in result.hits}
        assert "Mira" not in names
        assert "Ashbrand" not in names  # only reachable through Mira
        assert "The Undercroft" in names  # direct located_at edge survives

    async def test_edge_type_filter(self, repository, world: World):
        result = explore(
            repository.graph,
            ExploreQuery(
                start=world.mira.id,
                edge_types=frozenset({EdgeType.POSSESSES}),
            ),
        )
        names = {hit.node.name for hit in result.hits}
        assert names == {"Mira", "Ashbrand"}

    async def test_limit_truncates_and_flags(self, repository, world: World):
        result = explore(
            repository.graph, ExploreQuery(start=world.mira.id, depth=1, limit=2)
        )
        assert result.truncated is True
        assert len(result.hits) == 3  # start + limit

    async def test_depth_is_clamped_to_max(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id, depth=99))
        assert all(hit.depth <= 3 for hit in result.hits)
