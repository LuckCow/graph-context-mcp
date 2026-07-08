"""Presenters: detail shaping and derived views."""

from graph_context.domain.models import Node
from graph_context.domain.overview import GraphOverview, HubNode, TypeCount
from graph_context.domain.query import QueryResult, SortKey
from graph_context.domain.traversal import ExploreQuery, explore
from graph_context.interface.presenters import (
    Detail,
    render_explore_result,
    render_overview,
    render_query_result,
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


class TestQueryResult:
    def _todo(self, node_id: str, name: str, **fields: str) -> Node:
        return Node(id=node_id, type="Todo", name=name, summary="s.", fields=fields)

    def test_sort_key_values_are_echoed_on_each_line(self) -> None:
        result = QueryResult(
            hits=(self._todo("n1", "Buy milk", due_date="2026-07-10"),),
            matched=1,
            truncated=False,
        )
        text = render_query_result(
            result, Detail.SUMMARIES, order_by=(SortKey("due_date"),)
        )
        assert "query: 1 of 1 match(es)." in text
        assert "[due_date=2026-07-10]" in text

    def test_absent_sort_key_renders_as_none(self) -> None:
        result = QueryResult(
            hits=(self._todo("n1", "Buy milk"),), matched=1, truncated=False
        )
        text = render_query_result(
            result, Detail.NAMES, order_by=(SortKey("due_date"),)
        )
        assert "[due_date=(none)]" in text

    def test_truncation_footer_reports_shown_of_matched(self) -> None:
        result = QueryResult(
            hits=(self._todo("n1", "Buy milk"),), matched=7, truncated=True
        )
        text = render_query_result(result, Detail.NAMES)
        assert "query: 1 of 7 match(es)." in text
        assert "showing 1 of 7" in text and "`limit`" in text

    def test_zero_matches_returns_guidance_not_emptiness(self) -> None:
        text = render_query_result(
            QueryResult(hits=(), matched=0, truncated=False), Detail.SUMMARIES
        )
        assert "0 matches" in text and "`where`" in text


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
