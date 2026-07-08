"""Attribute-query semantics: predicates, coercion, ordering, anchoring.

The load-bearing contracts here mirror ``domain/query.py``'s docstring --
especially ``neq`` matching absent keys (an unticked Anytype checkbox is
stored as absence, so ``done neq true`` must find every open todo).
"""

import pytest

from graph_context.domain.models import NodeDraft
from graph_context.domain.query import (
    NodeQuery,
    Op,
    Predicate,
    QueryResult,
    SortKey,
    run_query,
)
from graph_context.domain.schema import Role
from graph_context.errors import GraphContextError, NodeNotFound
from tests.conftest import World


@pytest.fixture
def make_todo(writer):
    """Create a Todo node through the production write path."""

    async def _make(name: str, **fields: str):
        return await writer.create_node(
            NodeDraft("Todo", name=name, summary=f"{name}.", fields=fields)
        )

    return _make


def names(result: QueryResult) -> list[str]:
    return [node.name for node in result.hits]


class TestPredicates:
    async def test_neq_matches_nodes_lacking_the_field(self, repository, make_todo):
        await make_todo("Pay taxes", done="true")
        await make_todo("Buy milk")  # unticked checkbox = absent key
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("done", Op.NEQ, "true"),)),
        )
        assert names(result) == ["Buy milk"]

    async def test_eq_does_not_match_absent_fields(self, repository, make_todo):
        await make_todo("Pay taxes", done="true")
        await make_todo("Buy milk")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("done", Op.EQ, "false"),)),
        )
        assert names(result) == []

    async def test_missing_matches_only_absent_keys(self, repository, make_todo):
        await make_todo("Pay taxes", done="true")
        await make_todo("Buy milk")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("done", Op.MISSING),)),
        )
        assert names(result) == ["Buy milk"]

    async def test_exists_matches_only_present_keys(self, repository, make_todo):
        await make_todo("Pay taxes", done="true")
        await make_todo("Buy milk")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("done", Op.EXISTS),)),
        )
        assert names(result) == ["Pay taxes"]

    async def test_multiple_predicates_are_anded(self, repository, make_todo):
        await make_todo("Buy milk", priority="High")
        await make_todo("Call mom", priority="High", done="true")
        await make_todo("Write report", priority="Low")
        result = run_query(
            repository.graph,
            NodeQuery(
                predicates=(
                    Predicate("priority", Op.EQ, "High"),
                    Predicate("done", Op.NEQ, "true"),
                )
            ),
        )
        assert names(result) == ["Buy milk"]

    async def test_contains_matches_case_insensitively(self, repository, make_todo):
        await make_todo("Buy milk", notes="Oat Milk preferred")
        await make_todo("Call mom", notes="after work")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("notes", Op.CONTAINS, "OAT"),)),
        )
        assert names(result) == ["Buy milk"]

    async def test_builtin_fields_are_queryable(self, repository, make_todo):
        await make_todo("Buy milk")
        await make_todo("Call mom")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("name", Op.EQ, "buy milk"),)),
        )
        assert names(result) == ["Buy milk"]


class TestCoercion:
    async def test_numeric_strings_compare_numerically(self, repository, make_todo):
        await make_todo("Nine", rank="9")
        await make_todo("Ten", rank="10")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("rank", Op.LT, "10"),)),
        )
        assert names(result) == ["Nine"]  # lexicographic would say "10" < "9"

    async def test_eq_treats_equal_numbers_as_equal(self, repository, make_todo):
        await make_todo("Five", rank="5")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("rank", Op.EQ, "5.0"),)),
        )
        assert names(result) == ["Five"]

    async def test_iso_dates_order_chronologically(self, repository, make_todo):
        await make_todo("Later", due_date="2026-07-10")
        await make_todo("Sooner", due_date="2026-06-30")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("due_date", Op.LTE, "2026-06-30"),)),
        )
        assert names(result) == ["Sooner"]

    async def test_mixed_numeric_and_text_fall_back_to_string_order(
        self, repository, make_todo
    ):
        await make_todo("Numbered", label="2")
        await make_todo("Worded", label="ten")
        result = run_query(
            repository.graph,
            NodeQuery(predicates=(Predicate("label", Op.LT, "ten"),)),
        )
        assert names(result) == ["Numbered"]  # "2" < "ten" as strings


class TestOrdering:
    async def test_multi_key_sort_applies_keys_left_to_right(
        self, repository, make_todo
    ):
        await make_todo("B", due_date="2026-07-10", priority="2")
        await make_todo("A", due_date="2026-07-10", priority="1")
        await make_todo("C", due_date="2026-07-09", priority="3")
        result = run_query(
            repository.graph,
            NodeQuery(order_by=(SortKey("due_date"), SortKey("priority"))),
        )
        assert names(result) == ["C", "A", "B"]

    async def test_desc_reverses_only_its_own_key(self, repository, make_todo):
        await make_todo("B", due_date="2026-07-10", priority="2")
        await make_todo("A", due_date="2026-07-10", priority="1")
        await make_todo("C", due_date="2026-07-09", priority="3")
        result = run_query(
            repository.graph,
            NodeQuery(
                order_by=(SortKey("due_date"), SortKey("priority", descending=True))
            ),
        )
        assert names(result) == ["C", "B", "A"]

    async def test_nodes_missing_a_sort_key_sort_last(self, repository, make_todo):
        await make_todo("Dated", due_date="2026-07-10")
        await make_todo("Undated")
        ascending = run_query(
            repository.graph, NodeQuery(order_by=(SortKey("due_date"),))
        )
        descending = run_query(
            repository.graph,
            NodeQuery(order_by=(SortKey("due_date", descending=True),)),
        )
        assert names(ascending) == ["Dated", "Undated"]
        assert names(descending) == ["Dated", "Undated"]

    async def test_ties_break_by_name_then_id(self, repository, make_todo):
        await make_todo("Beta", priority="1")
        await make_todo("alpha", priority="1")
        result = run_query(
            repository.graph, NodeQuery(order_by=(SortKey("priority"),))
        )
        assert names(result) == ["alpha", "Beta"]  # casefolded name tie-break


class TestLimitAndScope:
    async def test_limit_cuts_after_sorting_and_reports_total_matched(
        self, repository, make_todo
    ):
        await make_todo("C", rank="3")
        await make_todo("A", rank="1")
        await make_todo("B", rank="2")
        result = run_query(
            repository.graph, NodeQuery(order_by=(SortKey("rank"),), limit=2)
        )
        assert names(result) == ["A", "B"]  # sorted before the cut
        assert result.matched == 3
        assert result.truncated is True

    async def test_excluded_roles_never_match(self, repository, make_todo):
        await make_todo("Buy milk")
        await repository.create_node(
            NodeDraft("gc_prose", name="Scene 1", summary="Captured text.")
        )
        result = run_query(
            repository.graph,
            NodeQuery(exclude_roles=frozenset({Role.CAPTURE})),
        )
        assert names(result) == ["Buy milk"]

    async def test_type_filter_matches_display_name_type_key_and_role(
        self, repository, make_todo
    ):
        await make_todo("Buy milk")
        prose = await repository.create_node(
            NodeDraft("gc_prose", name="Scene 1", summary="Captured text.")
        )
        for identifier in (prose.type, "gc_prose", "Capture"):
            result = run_query(
                repository.graph, NodeQuery(node_type=identifier)
            )
            assert names(result) == ["Scene 1"], identifier

    async def test_type_filter_is_case_insensitive(self, repository, make_todo):
        await make_todo("Buy milk")
        result = run_query(repository.graph, NodeQuery(node_type="todo"))
        assert names(result) == ["Buy milk"]


class TestAnchor:
    async def test_linked_to_restricts_candidates_to_neighbors(
        self, repository, world: World
    ):
        result = run_query(
            repository.graph, NodeQuery(linked_to=world.mira.id)
        )
        assert set(names(result)) == {
            "Siege of Brakk",
            "Fall of Brakk",
            "The Undercroft",
            "Ashbrand",
        }
        assert "Mira" not in names(result)  # the anchor itself is not a hit

    async def test_edge_types_constrain_the_anchor_adjacency(
        self, repository, world: World
    ):
        result = run_query(
            repository.graph,
            NodeQuery(
                linked_to=world.mira.id, edge_types=frozenset({"possesses"})
            ),
        )
        assert names(result) == ["Ashbrand"]

    async def test_character_timeline_orders_events_by_story_time(
        self, repository, world: World
    ):
        result = run_query(
            repository.graph,
            NodeQuery(
                node_type="Event",
                linked_to=world.mira.id,
                order_by=(SortKey("story_time"),),
            ),
        )
        assert names(result) == ["Siege of Brakk", "Fall of Brakk"]

    async def test_unknown_anchor_raises_node_not_found(self, repository, world):
        with pytest.raises(NodeNotFound):
            run_query(repository.graph, NodeQuery(linked_to="no-such-id"))


class TestFieldValidation:
    async def test_unknown_field_error_lists_observed_keys_for_the_type(
        self, repository, make_todo
    ):
        await make_todo("Buy milk", due_date="2026-07-10", priority="1")
        with pytest.raises(GraphContextError) as excinfo:
            run_query(
                repository.graph,
                NodeQuery(predicates=(Predicate("due", Op.EXISTS),)),
            )
        message = str(excinfo.value)
        assert "'due'" in message
        assert "due_date" in message and "priority" in message
        assert "story_time" in message  # built-ins always listed

    async def test_unknown_sort_field_is_also_validated(self, repository, make_todo):
        await make_todo("Buy milk", due_date="2026-07-10")
        with pytest.raises(GraphContextError):
            run_query(repository.graph, NodeQuery(order_by=(SortKey("deu_date"),)))

    async def test_empty_candidate_set_short_circuits_validation(
        self, repository, make_todo
    ):
        await make_todo("Buy milk")
        result = run_query(
            repository.graph,
            NodeQuery(
                node_type="Character",
                predicates=(Predicate("nonexistent", Op.EQ, "x"),),
            ),
        )
        assert result == QueryResult(hits=(), matched=0, truncated=False)
