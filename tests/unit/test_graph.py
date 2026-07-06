"""GraphIndex invariants: the single choke point for edges (open labels)."""

import pytest

from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Edge, Node
from graph_context.domain.schema import Role
from graph_context.errors import AmbiguousNodeName, NodeNotFound


def _node(node_id: str, node_type: str = "Character") -> Node:
    return Node(id=node_id, type=node_type, name=node_id, summary=f"{node_id} summary")


def _named(
    node_id: str, name: str, node_type: str = "Character", role: Role | None = None
) -> Node:
    return Node(id=node_id, type=node_type, name=name, summary="", role=role)


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


class TestNameResolution:
    """find_by_name / resolve: the LLM holds names, not Anytype CIDs."""

    @pytest.fixture
    def graph(self) -> GraphIndex:
        g = GraphIndex()
        g.upsert_node(_named("id-familinc", "FamiLinc", "Organization"))
        g.upsert_node(_named("id-mark", "Mark Kota"))
        g.upsert_node(_named("id-mara", "Mara Stone"))
        g.upsert_node(_named("id-epstein", "The Epstein Class", "Organization"))
        g.upsert_node(
            _named("id-prose", "FamiLinc scene", "Capture", role=Role.CAPTURE)
        )
        return g

    def test_resolve_passes_through_a_real_id(self, graph: GraphIndex) -> None:
        assert graph.resolve("id-mark").id == "id-mark"

    def test_resolve_exact_name_is_case_insensitive(self, graph: GraphIndex) -> None:
        # The transcript's "Familinc" vs "FamiLinc" capitalization mismatch.
        assert graph.resolve("familinc").id == "id-familinc"

    def test_resolve_unique_substring(self, graph: GraphIndex) -> None:
        assert graph.resolve("Kota").id == "id-mark"

    def test_resolve_unknown_raises_node_not_found(self, graph: GraphIndex) -> None:
        with pytest.raises(NodeNotFound):
            graph.resolve("nobody here")

    def test_resolve_ambiguous_substring_lists_candidates(
        self, graph: GraphIndex
    ) -> None:
        # "Mar" matches both Mark Kota and Mara Stone.
        with pytest.raises(AmbiguousNodeName) as excinfo:
            graph.resolve("Mar")
        ids = {c[2] for c in excinfo.value.candidates}
        assert ids == {"id-mark", "id-mara"}

    def test_exact_name_wins_over_substring(self, graph: GraphIndex) -> None:
        # An exact "Mara Stone" resolves even though "Mar" is ambiguous.
        assert graph.resolve("Mara Stone").id == "id-mara"

    def test_find_by_name_excludes_infra_roles(self, graph: GraphIndex) -> None:
        # The Prose node named "FamiLinc scene" must not surface on a bare name.
        matches = graph.find_by_name("FamiLinc")
        assert [n.id for n in matches] == ["id-familinc"]

    def test_find_by_name_type_filter(self, graph: GraphIndex) -> None:
        # "a" appears in both org names but the Character matches are filtered.
        orgs = graph.find_by_name("a", node_type="Organization")
        assert {n.id for n in orgs} == {"id-familinc", "id-epstein"}

    def test_find_by_name_empty_query(self, graph: GraphIndex) -> None:
        assert graph.find_by_name("   ") == []
