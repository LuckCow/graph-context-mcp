"""GraphIndex invariants: the single choke point for edges."""

import pytest

from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Edge, Node
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.errors import NodeNotFound, SchemaViolation


def _node(node_id: str, node_type: NodeType = NodeType.CHARACTER) -> Node:
    return Node(id=node_id, type=node_type, name=node_id, summary=f"{node_id} summary")


@pytest.fixture
def graph() -> GraphIndex:
    g = GraphIndex()
    g.upsert_node(_node("a"))
    g.upsert_node(_node("b"))
    g.upsert_node(_node("home", NodeType.LOCATION))
    return g


class TestEdges:
    def test_add_edge_requires_both_endpoints(self, graph: GraphIndex) -> None:
        with pytest.raises(NodeNotFound):
            graph.add_edge(Edge("a", EdgeType.KNOWS, "ghost"))

    def test_add_edge_enforces_schema_rules(self, graph: GraphIndex) -> None:
        with pytest.raises(SchemaViolation):
            graph.add_edge(Edge("home", EdgeType.KNOWS, "a"))

    def test_neighbors_sees_both_directions(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", EdgeType.KNOWS, "b"))
        assert [n.id for _, n in graph.neighbors("b")] == ["a"]
        assert list(graph.edges("b", Direction.OUT)) == []

    def test_edge_type_filter(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", EdgeType.KNOWS, "b"))
        graph.add_edge(Edge("a", EdgeType.LOCATED_AT, "home"))
        only = [e for e in graph.edges("a", edge_types=[EdgeType.LOCATED_AT])]
        assert [e.target for e in only] == ["home"]


class TestNodeRemoval:
    def test_removing_node_removes_incident_edges(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", EdgeType.KNOWS, "b"))
        graph.remove_node("b")
        assert graph.edge_count() == 0
        assert list(graph.edges("a")) == []

    def test_upsert_replaces_value_keeps_edges(self, graph: GraphIndex) -> None:
        graph.add_edge(Edge("a", EdgeType.KNOWS, "b"))
        graph.upsert_node(_node("a"))
        assert graph.edge_count() == 1
