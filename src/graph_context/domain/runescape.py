"""The RuneLite export format (ADR 062): documents -> desired nodes.

The plugin (``plugin_version`` 0.6.x) writes one JSON file per subject
-- ``character``, ``quests``, ``diaries``, ``combat_achievements``,
``equipment``, ``inventory`` -- each carrying ``exported_at`` and a
``reason``. This module maps each to nodes of a small set of native
types, ONE scalar property per leaf the rule engine should be able to
watch (``Skill · Level changed``, ``Quest · Complete changed to true``,
``Item Holding · Quantity changed``). Modelling choices (ADR 062):

* **Many small nodes of few types**, not one big Character: a rule
  watches one property on one type.
* **Equipment is slot-keyed** (the eleven worn slots always exist, so
  "Weapon changed" can be authored before anything is equipped);
  **inventory is per item name** (``Item Holding``: quantity summed
  across stacks -- the game reuses a display name across ids, bird
  nests being four ids), with a holding that left the bag written to
  quantity 0 rather than deleted (the port has no archive, and the zero
  transition IS the signal).
* **Combat achievements are tier nodes** (six); per-task nodes are a
  follow-up, not a switch.
* Names come from the game verbatim; a document's ``exported_at`` lands
  only on the Character (``Last export``), so every export changes
  exactly one node beyond what the game changed.

Pure: no I/O; malformed shapes raise :class:`ImportDocumentError` with
the document kind, and the importer skips that one document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graph_context.domain.imports import (
    DocumentNodes,
    ImportDocument,
    ImportDocumentError,
    ImportNode,
    ImportType,
    wire_value,
)
from graph_context.domain.models import PropertyDraft

CHARACTER = "Character"
SKILL = "Skill"
QUEST = "Quest"
DIARY = "Achievement Diary"
COMBAT_TIER = "Combat Achievement Tier"
EQUIPMENT = "Equipment Slot"
HOLDING = "Item Holding"

QUEST_STATES = ("Not started", "In progress", "Finished")
DIARY_TIERS = ("Easy", "Medium", "Hard", "Elite")
COMBAT_TIERS = ("Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster")

# RuneLite's EquipmentInventorySlot ordinals: the worn slots by index.
# Indices 6, 8 and 11 are unused by the client and never exported.
EQUIPMENT_SLOTS: Mapping[int, str] = {
    0: "Head", 1: "Cape", 2: "Amulet", 3: "Weapon", 4: "Body", 5: "Shield",
    7: "Legs", 9: "Gloves", 10: "Boots", 12: "Ring", 13: "Ammo",
}

# Display name -> format, per type. The one place the vocabulary lives:
# the ImportType drafts, the wire coercion, and the mapper all read it.
_FIELDS: Mapping[str, Mapping[str, str]] = {
    CHARACTER: {
        "World": "number", "Game state": "text", "Members": "checkbox",
        "Total level": "number", "Total experience": "number",
        "Last export": "text", "Export reason": "text",
        "Plugin version": "text",
    },
    SKILL: {"Level": "number", "Boosted level": "number", "Experience": "number"},
    QUEST: {"Quest id": "number", "Status": "select", "Complete": "checkbox"},
    DIARY: {
        "Region": "text", "Tier": "select", "Complete": "checkbox",
        "Tasks done": "number",
    },
    COMBAT_TIER: {
        "Complete": "checkbox", "Tasks completed": "number",
        "Tasks total": "number",
    },
    EQUIPMENT: {
        "Slot": "number", "Item": "text", "Item id": "number",
        "Quantity": "number",
    },
    HOLDING: {"Item id": "number", "Quantity": "number", "Stacks": "number"},
}
_OPTIONS: Mapping[tuple[str, str], tuple[str, ...]] = {
    (QUEST, "Status"): QUEST_STATES,
    (DIARY, "Tier"): DIARY_TIERS,
}
_PLURALS = {
    CHARACTER: "Characters", SKILL: "Skills", QUEST: "Quests",
    DIARY: "Achievement Diaries", COMBAT_TIER: "Combat Achievement Tiers",
    EQUIPMENT: "Equipment Slots", HOLDING: "Item Holdings",
}
_SUMMARIES = {
    CHARACTER: "Account state as exported by the RuneLite plugin.",
    SKILL: "A skill's levels and experience, as exported from the game.",
    QUEST: "A quest's progress, as exported from the game.",
    DIARY: "One achievement diary tier's progress, as exported from the game.",
    COMBAT_TIER: "One combat achievement tier's progress, as exported from the game.",
    EQUIPMENT: "What the character wears in this slot, as exported from the game.",
    HOLDING: "How much of one item the inventory holds, as exported from the game.",
}
# Kinds whose documents list EVERY member of a type, so unmentioned
# nodes of that type mean "gone" and take the type's absent_fields.
_COVERS = {"inventory": (HOLDING,)}


def _drafts(type_name: str) -> tuple[PropertyDraft, ...]:
    return tuple(
        PropertyDraft(name, fmt, options=_OPTIONS.get((type_name, name), ()))
        for name, fmt in _FIELDS[type_name].items()
    )


TYPES: tuple[ImportType, ...] = tuple(
    ImportType(
        name=type_name, plural=_PLURALS[type_name], properties=_drafts(type_name),
        summary=_SUMMARIES[type_name],
        absent_fields=(
            {"Quantity": "0", "Stacks": "0"} if type_name == HOLDING else {}
        ),
    )
    for type_name in _FIELDS
)


def _node(type_name: str, name: str, **values: Any) -> ImportNode:
    formats = _FIELDS[type_name]
    unknown = set(values) - set(formats)
    if unknown:  # a mapper bug, not a document problem -- fail the test
        raise ValueError(f"{type_name} has no property {sorted(unknown)}")
    return ImportNode(
        type=type_name, name=name,
        fields={key: wire_value(formats[key], value) for key, value in values.items()},
    )


class RuneScapeExports:
    """The :class:`~graph_context.domain.imports.ImportFormat` for the
    plugin's directory of JSON exports. Stateless."""

    @property
    def types(self) -> Sequence[ImportType]:
        return TYPES

    def nodes_for(self, document: ImportDocument) -> DocumentNodes:
        mapper = _MAPPERS.get(document.kind)
        if mapper is None:
            return DocumentNodes(())  # not ours: ignored, never an error
        try:
            nodes = mapper(document.payload)
        except (KeyError, TypeError, AttributeError, ValueError) as err:
            raise ImportDocumentError(
                f"{document.kind} export does not have the expected shape: "
                f"{err.__class__.__name__}: {err}"
            ) from None
        return DocumentNodes(tuple(nodes), _COVERS.get(document.kind, ()))


def _character(payload: Mapping[str, Any]) -> list[ImportNode]:
    stats = payload["stats"]
    nodes = [_node(
        CHARACTER, str(payload["account_name"]),
        **{
            "World": payload.get("world"),
            "Game state": payload.get("game_state", ""),
            "Members": "MEMBERS" in payload.get("world_types", ()),
            "Total level": sum(int(s["real_level"]) for s in stats.values()),
            "Total experience": sum(int(s["experience"]) for s in stats.values()),
            "Last export": payload.get("exported_at", ""),
            "Export reason": payload.get("reason", ""),
            "Plugin version": payload.get("plugin_version", ""),
        },
    )]
    for name, stat in stats.items():
        nodes.append(_node(
            SKILL, str(name),
            **{
                "Level": stat["real_level"],
                "Boosted level": stat["boosted_level"],
                "Experience": stat["experience"],
            },
        ))
    return nodes


def _quests(payload: Mapping[str, Any]) -> list[ImportNode]:
    states = {"NOT_STARTED": 0, "IN_PROGRESS": 1, "FINISHED": 2}
    nodes = []
    for quest in payload["quests"]:
        status = QUEST_STATES[states[str(quest["state"])]]
        nodes.append(_node(
            QUEST, str(quest["name"]),
            **{
                "Quest id": quest["id"], "Status": status,
                "Complete": status == "Finished",
            },
        ))
    return nodes


def _diaries(payload: Mapping[str, Any]) -> list[ImportNode]:
    nodes = []
    for region, tiers in payload["diaries"].items():
        for tier, progress in tiers.items():
            label = str(tier).capitalize()
            nodes.append(_node(
                DIARY, f"{region} {label}",
                **{
                    "Region": str(region), "Tier": label,
                    "Complete": bool(progress["complete"]),
                    "Tasks done": progress["tasks_done"],
                },
            ))
    return nodes


def _combat(payload: Mapping[str, Any]) -> list[ImportNode]:
    return [
        _node(
            COMBAT_TIER, str(tier).capitalize(),
            **{
                "Complete": bool(progress["complete"]),
                "Tasks completed": progress["tasks_completed"],
                "Tasks total": progress["tasks_total"],
            },
        )
        for tier, progress in payload["tiers"].items()
    ]


def _equipment(payload: Mapping[str, Any]) -> list[ImportNode]:
    worn = {int(item["slot"]): item for item in payload["items"]}
    nodes = []
    for slot, label in EQUIPMENT_SLOTS.items():
        item = worn.get(slot)
        nodes.append(_node(
            EQUIPMENT, label,
            **{
                "Slot": slot,
                "Item": str(item["name"]) if item else "",
                "Item id": item["id"] if item else 0,
                "Quantity": item["quantity"] if item else 0,
            },
        ))
    return nodes


def _inventory(payload: Mapping[str, Any]) -> list[ImportNode]:
    holdings: dict[str, dict[str, int]] = {}
    for item in payload["items"]:
        held = holdings.setdefault(
            str(item["name"]), {"Item id": int(item["id"]), "Quantity": 0, "Stacks": 0}
        )
        held["Item id"] = min(held["Item id"], int(item["id"]))
        held["Quantity"] += int(item["quantity"])
        held["Stacks"] += 1
    return [_node(HOLDING, name, **held) for name, held in holdings.items()]


_MAPPERS = {
    "character": _character,
    "quests": _quests,
    "diaries": _diaries,
    "combat_achievements": _combat,
    "equipment": _equipment,
    "inventory": _inventory,
}
