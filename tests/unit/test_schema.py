"""Schema: role resolution and node-creation invariants (open vocabulary)."""

import pytest

from graph_context.domain import schema
from graph_context.domain.schema import Role
from graph_context.errors import SchemaViolation


class TestResolveRole:
    def test_native_type_key_resolves(self) -> None:
        assert schema.resolve_role("character") is Role.CHARACTER
        assert schema.resolve_role("event") is Role.EVENT

    def test_legacy_gc_key_resolves_for_read_compat(self) -> None:
        assert schema.resolve_role("gc_character") is Role.CHARACTER
        assert schema.resolve_role("gc_faction") is Role.ORGANIZATION

    def test_matching_is_case_insensitive_and_accepts_role_name(self) -> None:
        assert schema.resolve_role("Character") is Role.CHARACTER
        assert schema.resolve_role("  EVENT ") is Role.EVENT

    def test_unmapped_type_is_neutral(self) -> None:
        assert schema.resolve_role("realization") is None

    def test_overrides_win(self) -> None:
        role = schema.resolve_role("realization", {"realization": Role.EVENT})
        assert role is Role.EVENT


class TestValidateNewNode:
    def test_summary_is_required(self) -> None:
        with pytest.raises(SchemaViolation, match="summary"):
            schema.validate_new_node(Role.CHARACTER, "Mira", "   ", None)

    def test_name_is_required(self) -> None:
        with pytest.raises(SchemaViolation, match="name"):
            schema.validate_new_node(Role.CHARACTER, "  ", "An engineer.", None)

    def test_event_requires_story_time(self) -> None:
        with pytest.raises(SchemaViolation, match="story_time"):
            schema.validate_new_node(Role.EVENT, "Siege", "A siege.", None)

    def test_non_event_does_not_require_story_time(self) -> None:
        schema.validate_new_node(Role.CHARACTER, "Mira", "An engineer.", None)

    def test_neutral_role_does_not_require_story_time(self) -> None:
        schema.validate_new_node(None, "A realization", "Realized something.", None)
