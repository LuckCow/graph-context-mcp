"""SpaceRegistry: type/relation resolution for the space-reflecting model."""

from __future__ import annotations

from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype.registry import PropertyInfo, SpaceRegistry


def _registry() -> SpaceRegistry:
    return SpaceRegistry(
        types_by_key={
            "character": "Character",
            "event": "Event",
            "realization": "Realization",  # unmapped role
            "gc_prose": "Prose",
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

    def test_known_node_types_excludes_infra(self) -> None:
        reg = _registry()
        known = reg.known_node_types()
        assert "Character" in known and "Realization" in known
        assert "Prose" not in known  # infra role hidden


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
