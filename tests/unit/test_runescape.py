"""The RuneLite export mapper (ADR 062), over the plugin's real files.

``tests/fixtures/runescape/`` is one full export set (plugin 0.6.0).
The tests pin the modelling decisions: one node per leaf-bearing
subject, slot-keyed equipment with every worn slot present, per-name
inventory holdings, tier-level combat achievements, and lenient
handling of documents that are not ours or not shaped as promised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_context.domain import runescape
from graph_context.domain.imports import ImportDocument, ImportDocumentError
from graph_context.domain.runescape import RuneScapeExports

FIXTURES = Path(__file__).parent.parent / "fixtures" / "runescape"
FORMAT = RuneScapeExports()


def _doc(kind: str) -> ImportDocument:
    return ImportDocument(kind, json.loads((FIXTURES / f"{kind}.json").read_text()))


def _by_name(kind: str) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (n.type, n.name): dict(n.fields) for n in FORMAT.nodes_for(_doc(kind)).nodes
    }


class TestTypes:
    def test_every_type_declares_its_full_property_vocabulary(self) -> None:
        names = {t.name for t in FORMAT.types}
        assert names == {
            "Character", "Skill", "Quest", "Achievement Diary",
            "Combat Achievement Tier", "Equipment Slot", "Item Holding",
        }
        quest = next(t for t in FORMAT.types if t.name == "Quest")
        status = next(p for p in quest.properties if p.name == "Status")
        assert status.format == "select"
        assert status.options == runescape.QUEST_STATES

    def test_only_holdings_zero_out_when_unmentioned(self) -> None:
        absent = {t.name: dict(t.absent_fields) for t in FORMAT.types if t.absent_fields}
        assert absent == {"Item Holding": {"Quantity": "0", "Stacks": "0"}}

    def test_every_mapped_field_is_a_declared_property(self) -> None:
        declared = {t.name: {p.name for p in t.properties} for t in FORMAT.types}
        for kind in ("character", "quests", "diaries", "combat_achievements",
                     "equipment", "inventory"):
            for node in FORMAT.nodes_for(_doc(kind)).nodes:
                assert set(node.fields) <= declared[node.type], (kind, node)


class TestCharacter:
    def test_the_account_is_one_character_node_with_derived_totals(self) -> None:
        nodes = _by_name("character")
        character = nodes[("Character", "LuckCow")]
        assert character["World"] == "369"
        assert character["Members"] == "true"
        assert character["Game state"] == "LOGGED_IN"
        assert character["Export reason"] == "stat_changed"
        assert character["Last export"].startswith("2026-09-06T17:28:32")
        assert character["Total level"] == "1634"
        assert character["Total experience"] == "41065525"

    def test_each_stat_is_a_skill_node(self) -> None:
        nodes = _by_name("character")
        assert nodes[("Skill", "Attack")] == {
            "Level": "82", "Boosted level": "81", "Experience": "2439484",
        }
        assert nodes[("Skill", "Sailing")]["Experience"] == "0"
        assert sum(1 for t, _ in nodes if t == "Skill") == 24


class TestQuests:
    def test_states_map_to_the_select_and_a_complete_checkbox(self) -> None:
        nodes = _by_name("quests")
        assert nodes[("Quest", "Animal Magnetism")] == {
            "Quest id": "0", "Status": "Finished", "Complete": "true",
        }
        assert nodes[("Quest", "Bear Your Soul")]["Status"] == "In progress"
        assert nodes[("Quest", "Bear Your Soul")]["Complete"] == "false"
        assert nodes[("Quest", "Below Ice Mountain")]["Status"] == "Not started"
        assert len(nodes) == 211


class TestDiariesAndCombat:
    def test_each_region_tier_is_a_node_named_region_tier(self) -> None:
        nodes = _by_name("diaries")
        assert nodes[("Achievement Diary", "Ardougne Hard")] == {
            "Region": "Ardougne", "Tier": "Hard", "Complete": "false",
            "Tasks done": "1",
        }
        assert len(nodes) == 48

    def test_combat_achievements_are_tier_nodes(self) -> None:
        nodes = _by_name("combat_achievements")
        assert set(n for _, n in nodes) == set(runescape.COMBAT_TIERS)
        assert nodes[("Combat Achievement Tier", "Easy")] == {
            "Complete": "true", "Tasks completed": "2", "Tasks total": "41",
        }


class TestEquipment:
    def test_every_worn_slot_exists_whether_or_not_it_is_filled(self) -> None:
        nodes = _by_name("equipment")
        assert set(n for _, n in nodes) == set(runescape.EQUIPMENT_SLOTS.values())
        assert nodes[("Equipment Slot", "Weapon")] == {
            "Slot": "3", "Item": "Dual macuahuitl", "Item id": "28997", "Quantity": "1",
        }
        assert nodes[("Equipment Slot", "Shield")] == {
            "Slot": "5", "Item": "", "Item id": "0", "Quantity": "0",
        }


class TestInventory:
    def test_holdings_aggregate_by_item_name_across_stacks_and_ids(self) -> None:
        mapped = FORMAT.nodes_for(_doc("inventory"))
        assert mapped.covers == ("Item Holding",)
        nodes = {n.name: dict(n.fields) for n in mapped.nodes}
        # Four nest ids share one display name in the game.
        assert nodes["Bird nest"] == {"Item id": "5071", "Quantity": "8", "Stacks": "8"}
        assert nodes["Potato seed"] == {"Item id": "5318", "Quantity": "1779", "Stacks": "1"}
        assert nodes["Uncut opal"]["Stacks"] == "3"
        assert len(nodes) == 14


class TestLeniency:
    def test_a_document_of_another_kind_maps_to_nothing(self) -> None:
        assert FORMAT.nodes_for(ImportDocument("bank", {"x": 1})).nodes == ()

    @pytest.mark.parametrize("kind", ["character", "quests", "diaries",
                                      "combat_achievements", "equipment", "inventory"])
    def test_a_misshapen_document_raises_naming_its_kind(self, kind: str) -> None:
        with pytest.raises(ImportDocumentError, match=kind):
            FORMAT.nodes_for(ImportDocument(kind, {"exported_at": "x"}))

    def test_an_unknown_quest_state_is_a_document_error(self) -> None:
        payload = {"quests": [{"id": 1, "name": "Q", "state": "WEIRD"}]}
        with pytest.raises(ImportDocumentError, match="quests"):
            FORMAT.nodes_for(ImportDocument("quests", payload))
