"""GraphIndex invariants: the single choke point for edges (open labels)."""

import pytest

from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Edge, Node
from graph_context.errors import NodeNotFound


def _node(node_id: str, node_type: str = "Character") -> Node:
    return Node(id=node_id, type=node_type, name=node_id, summary=f"{node_id} summary")


@pytest.fixture
def graph() -> GraphIndex:
    g = GraphIndex()
    g.upsert_node(_node("a"))
    g.upsert_node(_node("b"))
    g.upsert_node(_node("home", "Location"))
    return g


class TestEdges:
    def test_add_edge_requires_both_endpoints(self, graph: GraphIndex) -> None:
        with pytest.raises(NodeNotFound):
            graph.add_edge(Edge("a", "knows", "ghost"))

    def test_any_label_is_admissible(self, graph: GraphIndex) -> None:
        # Open vocabulary: a custom relation label is accepted as-is.
        graph.add_edge(Edge("home", "boss", "a"))
        assert [e.type for e in graph.edges("home", Direction.OUT)] == ["boss"]

    def test_self_loop_is_skipped(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "links", "a"))
        assert list(graph.edges("a")) == []

    def test_neighbors_sees_both_directions(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b"))
        assert [n.id for _, n in graph.neighbors("b")] == ["a"]
        assert list(graph.edges("b", Direction.OUT)) == []

    def test_edge_type_filter(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b"))
        graph.add_edge(Edge("a", "located_at", "home"))
        only = list(graph.edges("a", edge_types=["located_at"]))
        assert [e.target for e in only] == ["home"]

    def test_distinct_property_keys_are_distinct_edges(self, graph: GraphIndex) -> None:
        # Same endpoints, two different relation channels -> two edges.
        graph.add_edge(Edge("a", "knows", "b", property_key="gc_edge_knows"))
        graph.add_edge(Edge("a", "knows", "b", property_key="custom_knows"))
        assert graph.edge_count() == 2

    def test_identical_edges_dedupe(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b", property_key="gc_edge_knows"))
        graph.add_edge(Edge("a", "knows", "b", property_key="gc_edge_knows"))
        assert graph.edge_count() == 1


class TestDegree:
    def test_degree_counts_both_directions(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b"))
        graph.add_edge(Edge("a", "located_at", "home"))  # a: 2 out
        graph.add_edge(Edge("b", "knows", "a"))          # a: 1 in
        assert graph.degree("a") == 3

    def test_isolated_node_has_degree_zero(self, graph: GraphIndex) -> None:
        assert graph.degree("a") == 0

    def test_unknown_id_has_degree_zero(self, graph: GraphIndex) -> None:
        assert graph.degree("ghost") == 0


class TestNodeRemoval:
    def test_removing_node_removes_incident_edges(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b"))
        graph.remove_node("b")
        assert graph.edge_count() == 0
        assert list(graph.edges("a")) == []

    def test_upsert_replaces_value_keeps_edges(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", "knows", "b"))
        graph.upsert_node(_node("a"))
        assert graph.edge_count() == 1
