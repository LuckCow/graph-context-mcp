"""Presenters: detail shaping and derived views."""

from graph_context.domain.models import Node
from graph_context.domain.overview import GraphOverview, HubNode, TypeCount
from graph_context.domain.traversal import ExploreQuery, explore
from graph_context.interface.presenters import (
    Detail,
    render_explore_result,
    render_overview,
)
from tests.conftest import World


class TestDetailLevels:
    async def test_names_detail_omits_summaries(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id))
        text = render_explore_result(result, Detail.NAMES)
        assert "Exiled siege engineer" not in text and "Mira" in text

    async def test_summaries_detail_includes_one_liners(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id))
        text = render_explore_result(result, Detail.SUMMARIES)
        assert "Exiled siege engineer." in text

    async def test_truncation_notice_is_rendered(self, repository, world: World):
        result = explore(repository.graph, ExploreQuery(start=world.mira.id, limit=1))
        text = render_explore_result(result, Detail.NAMES)
        assert "limit reached" in text


class TestOverview:
    def test_populated_overview_surfaces_types_and_hub_ids(self) -> None:
        mira = Node(id="n1", type="Character", name="Mira", summary="Engineer.")
        overview = GraphOverview(
            total_story_nodes=3,
            type_counts=(TypeCount("Character", 2), TypeCount("Location", 1)),
            hubs=(HubNode(mira, degree=4),),
        )
        text = render_overview(overview)
        assert "types:" in text and "Character 2" in text
        assert "entry points" in text
        assert "id=n1" in text and "Mira" in text

    def test_empty_overview_guides_to_create_node(self) -> None:
        overview = GraphOverview(total_story_nodes=0, type_counts=(), hubs=())
        assert "no nodes yet" in render_overview(overview)
