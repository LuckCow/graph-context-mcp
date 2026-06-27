"""build_overview: the derived cold-start entry-point map."""

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, Node
from graph_context.domain.overview import build_overview
from graph_context.domain.schema import Role


def _node(node_id: str, node_type: str = "Character", role: Role | None = None) -> Node:
    return Node(
        id=node_id, type=node_type, name=node_id,
        summary=f"{node_id} summary", role=role,
    )


def _world() -> GraphIndex:
    """A small graph where ``hub`` is the clear highest-degree node."""
    g = GraphIndex()
    g.upsert_node(_node("hub", "Character", Role.CHARACTER))
    g.upsert_node(_node("loc", "Location", Role.LOCATION))
    g.upsert_node(_node("ev1", "Event", Role.EVENT))
    g.upsert_node(_node("ev2", "Event", Role.EVENT))
    g.add_edge(Edge("hub", "located_at", "loc"))
    g.add_edge(Edge("hub", "in", "ev1"))
    g.add_edge(Edge("hub", "in", "ev2"))  # hub: degree 3
    return g


class TestStoryFiltering:
    def test_infra_roles_excluded_from_counts_and_hubs(self) -> None:
        g = _world()
        g.upsert_node(_node("p", "Prose", Role.PROSE))
        g.upsert_node(_node("s", "SessionContext", Role.SESSION_CONTEXT))
        g.add_edge(Edge("p", "references", "hub"))  # give prose a high degree

        overview = build_overview(g)

        assert overview.total_story_nodes == 4  # hub, loc, ev1, ev2
        types = {tc.type for tc in overview.type_counts}
        assert "Prose" not in types and "SessionContext" not in types
        hub_ids = {h.node.id for h in overview.hubs}
        assert "p" not in hub_ids and "s" not in hub_ids


class TestTypeCounts:
    def test_sorted_by_count_then_type_name(self) -> None:
        overview = build_overview(_world())
        # Event 2 (highest) first; Character 1 and Location 1 tie -> by name.
        assert [(tc.type, tc.count) for tc in overview.type_counts] == [
            ("Event", 2),
            ("Character", 1),
            ("Location", 1),
        ]


class TestHubRanking:
    def test_highest_degree_node_ranks_first(self) -> None:
        overview = build_overview(_world())
        assert overview.hubs[0].node.id == "hub"
        assert overview.hubs[0].degree == 3

    def test_equal_degree_breaks_tie_by_name(self) -> None:
        # loc, ev1, ev2 all have degree 1 -> ordered by name: ev1, ev2, loc.
        overview = build_overview(_world())
        tail = [h.node.id for h in overview.hubs[1:]]
        assert tail == ["ev1", "ev2", "loc"]

    def test_hub_limit_bounds_the_list(self) -> None:
        overview = build_overview(_world(), hub_limit=2)
        assert len(overview.hubs) == 2


class TestEmptyGraph:
    def test_empty_graph_yields_zeros(self) -> None:
        overview = build_overview(GraphIndex())
        assert overview.total_story_nodes == 0
        assert overview.type_counts == ()
        assert overview.hubs == ()
