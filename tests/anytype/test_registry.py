"""SpaceRegistry: type/relation resolution for the space-reflecting model."""

from __future__ import annotations

from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype.registry import (
    PropertyInfo,
    SpaceRegistry,
    TypeInfo,
)


def _registry() -> SpaceRegistry:
    return SpaceRegistry(
        types_by_key={
            "character": TypeInfo("character", "Character", id="type-character"),
            "event": TypeInfo("event", "Event"),
            "realization": TypeInfo("realization", "Realization"),  # unmapped role
            "gc_prose": TypeInfo("gc_prose", "Prose"),
        },
        properties_by_key={
            "gc_edge_knows": PropertyInfo("gc_edge_knows", "edge: knows", "objects"),
            "boss": PropertyInfo("boss", "boss", "objects"),
            "triggered_by": PropertyInfo("triggered_by", "Triggered By", "objects"),
            "backlinks": PropertyInfo("backlinks", "Backlinks", "objects"),
            "gc_summary": PropertyInfo("gc_summary", "gc_summary", "text"),
        },
    )


class TestTypes:
    def test_type_name_and_role(self) -> None:
        reg = _registry()
        assert reg.type_name("character") == "Character"
        assert reg.role_for("character") is Role.CHARACTER
        assert reg.role_for("realization") is None  # unmapped is neutral

    def test_type_key_for_matches_key_name_and_role(self) -> None:
        reg = _registry()
        assert reg.type_key_for("character") == "character"
        assert reg.type_key_for("Character") == "character"  # display name
        assert reg.type_key_for("Event") == "event"
        assert reg.type_key_for("nonsense") is None

    def test_type_id_for_returns_id_or_none(self) -> None:
        reg = _registry()
        assert reg.type_id_for("character") == "type-character"
        assert reg.type_id_for("event") is None  # no id captured
        assert reg.type_id_for("nonsense") is None  # unknown key

    def test_known_node_types_excludes_infra(self) -> None:
        reg = _registry()
        known = reg.known_node_types()
        assert "Character" in known and "Realization" in known
        assert "Capture" not in known  # infra role hidden

    def test_role_overrides_resolve_arbitrary_type_keys(self) -> None:
        # Profile-supplied overrides (WP5) map any type key to a role.
        reg = SpaceRegistry(
            types_by_key={"persona": TypeInfo("persona", "Persona")},
            role_overrides={"persona": Role.CHARACTER},
        )
        assert reg.role_for("persona") is Role.CHARACTER


class TestFieldCatalog:
    """ADR 023: per-type property lists and the reflectable views."""

    def _registry_with_type_props(self) -> SpaceRegistry:
        return SpaceRegistry(
            types_by_key={
                "task": TypeInfo(
                    "task", "Task", id="type-task",
                    properties=(
                        PropertyInfo("due_date", "Due date", "date", id="p-due"),
                        PropertyInfo("status", "Status", "select", id="p-status"),
                        PropertyInfo("assignee", "Assignee", "objects", id="p-a"),
                        PropertyInfo("created_date", "Created", "date", id="p-c"),
                    ),
                ),
            },
            properties_by_key={
                "due_date": PropertyInfo("due_date", "Due date", "date", id="p-due"),
                "status": PropertyInfo("status", "Status", "select", id="p-status"),
                "fuel": PropertyInfo("fuel", "fuel", "text", id="p-fuel"),
            },
        )

    def test_reflectable_type_properties_filter_edges_and_noise(self) -> None:
        reg = self._registry_with_type_props()
        assert [p.key for p in reg.reflectable_type_properties("task")] == [
            "due_date", "status",
        ]

    def test_reflectable_type_properties_of_unknown_type_is_empty(self) -> None:
        assert self._registry_with_type_props().reflectable_type_properties("x") == ()

    def test_reflectable_properties_is_the_write_match_universe(self) -> None:
        reg = self._registry_with_type_props()
        assert [p.key for p in reg.reflectable_properties()] == [
            "due_date", "fuel", "status",
        ]

    def test_types_without_properties_key_tolerated(self) -> None:
        # load_registry builds TypeInfo with properties=() when GET /types
        # items carry no list; the helpers must not blow up on those.
        assert _registry().reflectable_type_properties("character") == ()


class TestRelations:
    def test_label_for_strips_prefixes(self) -> None:
        reg = _registry()
        assert reg.label_for("gc_edge_knows") == "knows"
        assert reg.label_for("triggered_by") == "triggered_by"
        assert reg.label_for("boss") == "boss"

    def test_key_for_label_round_trips(self) -> None:
        reg = _registry()
        assert reg.key_for_label("knows") == "gc_edge_knows"
        assert reg.key_for_label("triggered_by") == "triggered_by"
        assert reg.key_for_label("boss") == "boss"
        assert reg.key_for_label("Boss") == "boss"  # case-insensitive

    def test_key_for_label_matches_the_display_name(self) -> None:
        """The human-visible name resolves too (like field_property):
        'Triggered By' must find triggered_by even though the cleaned
        key spells it with an underscore."""
        assert _registry().key_for_label("Triggered By") == "triggered_by"

    def test_key_for_label_misses_unknown_and_denylisted(self) -> None:
        reg = _registry()
        assert reg.key_for_label("mentored_by") is None
        assert reg.key_for_label("backlinks") is None  # denylisted system relation

    def test_known_edge_labels(self) -> None:
        reg = _registry()
        labels = reg.known_edge_labels()
        assert {"knows", "boss", "triggered_by"} <= labels
        assert "backlinks" not in labels
        assert "summary" not in labels  # not an objects relation
