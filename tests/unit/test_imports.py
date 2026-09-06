"""The snapshot-import planner (ADR 062): desired nodes vs the live index.

The invariants: a node the graph lacks is a create; an existing node
yields ONLY the fields whose value differs (change-only, so an unchanged
export costs no writes and an unrelated human edit is left alone);
absence writes ride the type's ``absent_fields`` and only for types the
batch covered; and values compare in read-back form (absent checkbox ==
"false", display names resolve through the catalog to store keys).
"""

from __future__ import annotations

from graph_context.domain.graph import GraphIndex
from graph_context.domain.imports import (
    ImportNode,
    ImportType,
    plan,
    wire_value,
)
from graph_context.domain.models import FieldSpec, Node, PropertyDraft
from graph_context.domain.schema import Role

SKILL = ImportType(
    name="Skill", plural="Skills",
    properties=(PropertyDraft("Level", "number"), PropertyDraft("Experience", "number")),
)
HOLDING = ImportType(
    name="Item Holding", plural="Item Holdings",
    properties=(PropertyDraft("Quantity", "number"), PropertyDraft("Complete", "checkbox")),
    absent_fields={"Quantity": "0"},
)
TYPES = (SKILL, HOLDING)


def _index(*nodes: Node) -> GraphIndex:
    index = GraphIndex()
    for node in nodes:
        index.upsert_node(node)
    return index


def _node(id: str, type: str, name: str, **fields: str) -> Node:  # noqa: A002
    return Node(id=id, type=type, type_key=type.lower(), name=name, summary="", fields=fields)


class TestWireValue:
    def test_numbers_render_untrailed_like_the_store_reads_them_back(self) -> None:
        assert wire_value("number", 82) == "82"
        assert wire_value("number", 82.0) == "82"
        assert wire_value("number", 1.5) == "1.5"

    def test_checkbox_and_none_take_their_wire_spellings(self) -> None:
        assert wire_value("checkbox", True) == "true"
        assert wire_value("checkbox", 0) == "false"
        assert wire_value("text", None) == ""


class TestPlan:
    def test_a_missing_node_is_a_create(self) -> None:
        want = ImportNode("Skill", "Attack", {"Level": "82", "Experience": "100"})
        result = plan([want], TYPES, [], _index(), {})
        assert result.creates == (want,)
        assert result.updates == ()

    def test_an_unchanged_node_yields_no_write(self) -> None:
        held = _node("n1", "Skill", "Attack", Level="82", Experience="100")
        want = ImportNode("Skill", "Attack", {"Level": "82", "Experience": "100"})
        assert plan([want], TYPES, [], _index(held), {}).empty

    def test_only_the_differing_fields_are_written(self) -> None:
        held = _node("n1", "Skill", "Attack", Level="82", Experience="100")
        want = ImportNode("Skill", "Attack", {"Level": "82", "Experience": "150"})
        (update,) = plan([want], TYPES, [], _index(held), {}).updates
        assert update.node_id == "n1"
        assert dict(update.fields) == {"Experience": "150"}

    def test_matching_is_case_insensitive_on_type_and_name(self) -> None:
        held = _node("n1", "skill", "attack", Level="82", Experience="100")
        want = ImportNode("Skill", "Attack", {"Level": "83", "Experience": "100"})
        result = plan([want], TYPES, [], _index(held), {})
        assert result.creates == ()
        assert result.updates[0].node_id == "n1"

    def test_display_names_resolve_to_store_keys_through_the_catalog(self) -> None:
        """The Anytype adapter stores fields under raw property keys; the
        catalog is how a display name finds its stored value."""
        held = _node("n1", "Skill", "Attack", level="82", experience="100")
        catalog = {"Skill": (
            FieldSpec("Level", "number", key="level"),
            FieldSpec("Experience", "number", key="experience"),
        )}
        want = ImportNode("Skill", "Attack", {"Level": "82", "Experience": "100"})
        assert plan([want], TYPES, [], _index(held), catalog).empty

    def test_a_verbatim_display_name_key_still_compares(self) -> None:
        """The open-vocabulary backend stores under the display name even
        when its catalog reports a slug key: no phantom update."""
        held = _node("n1", "Skill", "Attack", **{"Boosted level": "81"})
        catalog = {"Skill": (FieldSpec("Boosted level", "number", key="boosted_level"),)}
        boosted = ImportType(
            name="Skill", plural="Skills",
            properties=(PropertyDraft("Boosted level", "number"),),
        )
        want = ImportNode("Skill", "Attack", {"Boosted level": "81"})
        assert plan([want], (boosted,), [], _index(held), catalog).empty

    def test_an_absent_checkbox_reads_as_false(self) -> None:
        """The adapter drops unticked checkboxes from Node.fields; writing
        "false" onto absence would be a phantom change every tick."""
        held = _node("n1", "Item Holding", "Coins", Quantity="5")
        want = ImportNode("Item Holding", "Coins", {"Quantity": "5", "Complete": "false"})
        assert plan([want], TYPES, [], _index(held), {}).empty

    def test_a_human_edit_is_written_back_the_game_is_ground_truth(self) -> None:
        held = _node("n1", "Skill", "Attack", Level="99", Experience="100")
        want = ImportNode("Skill", "Attack", {"Level": "82", "Experience": "100"})
        (update,) = plan([want], TYPES, [], _index(held), {}).updates
        assert dict(update.fields) == {"Level": "82"}

    def test_infra_nodes_and_foreign_types_never_match(self) -> None:
        rule = Node(
            id="r1", type="Skill", type_key="gc_rule", name="Attack",
            summary="", role=Role.RULE,
        )
        other = _node("n2", "Quest", "Attack")
        want = ImportNode("Skill", "Attack", {"Level": "82"})
        result = plan([want], TYPES, [], _index(rule, other), {})
        assert result.creates == (want,)

    def test_duplicates_resolve_to_the_lowest_id(self) -> None:
        a = _node("n1", "Skill", "Attack", Level="1")
        b = _node("n2", "Skill", "Attack", Level="1")
        want = ImportNode("Skill", "Attack", {"Level": "2"})
        (update,) = plan([want], TYPES, [], _index(b, a), {}).updates
        assert update.node_id == "n1"


class TestAbsence:
    def test_an_unmentioned_node_of_a_covered_type_takes_absent_fields(self) -> None:
        gone = _node("n1", "Item Holding", "Bird nest", Quantity="7")
        result = plan([], TYPES, ["Item Holding"], _index(gone), {})
        (update,) = result.updates
        assert update.node_id == "n1" and dict(update.fields) == {"Quantity": "0"}

    def test_absence_is_change_only_too(self) -> None:
        gone = _node("n1", "Item Holding", "Bird nest", Quantity="0")
        assert plan([], TYPES, ["Item Holding"], _index(gone), {}).empty

    def test_an_uncovered_type_is_left_alone(self) -> None:
        """The inventory document failed to decode this tick: its
        holdings must not read as gone."""
        gone = _node("n1", "Item Holding", "Bird nest", Quantity="7")
        assert plan([], TYPES, [], _index(gone), {}).empty

    def test_a_type_without_absent_fields_is_never_touched(self) -> None:
        skill = _node("n1", "Skill", "Sailing", Level="1")
        assert plan([], TYPES, ["Skill"], _index(skill), {}).empty
