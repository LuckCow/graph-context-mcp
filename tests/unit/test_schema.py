"""Schema: role resolution and node-creation invariants (open vocabulary)."""

import pytest

from graph_context.domain import schema
from graph_context.domain.schema import Role
from graph_context.errors import SchemaViolation


class TestResolveRole:
    def test_native_type_key_resolves(self) -> None:
        assert schema.resolve_role("character") is Role.CHARACTER
        assert schema.resolve_role("event") is Role.EVENT

    def test_gc_infra_keys_resolve(self) -> None:
        assert schema.resolve_role("gc_prose") is Role.CAPTURE
        assert schema.resolve_role("gc_session_context") is Role.SESSION_CONTEXT
        assert schema.resolve_role("gc_activity_mode") is Role.MODE

    def test_mode_display_name_resolves_like_the_key(self) -> None:
        # Backends without a key registry (in-memory, eval worlds) carry
        # the display name as the type; a mode object must be infra-hidden
        # there too, or find_node sees different worlds per backend.
        assert schema.resolve_role("Activity Mode") is Role.MODE

    def test_mode_role_is_infra_hidden(self) -> None:
        # ADR 015 amendment: mode config objects are the human's editing
        # surface, never the LLM's traversal data.
        assert Role.MODE in schema.INFRA_ROLES

    def test_legacy_gc_entity_keys_are_not_domain_knowledge(self) -> None:
        # The pre-pivot closed gc_ entity types are gone (ADR 006/028);
        # the domain map never knew them.
        assert schema.resolve_role("gc_character") is None

    def test_matching_is_case_insensitive_and_accepts_role_name(self) -> None:
        assert schema.resolve_role("Character") is Role.CHARACTER
        assert schema.resolve_role("  EVENT ") is Role.EVENT

    def test_unmapped_type_is_neutral(self) -> None:
        assert schema.resolve_role("realization") is None

    def test_overrides_win(self) -> None:
        role = schema.resolve_role("realization", {"realization": Role.EVENT})
        assert role is Role.EVENT


class TestValidateFieldDeclarations:
    def test_empty_declarations_are_a_noop(self) -> None:
        schema.validate_field_declarations({"due": "2026-08-01"}, {})

    def test_valid_declaration_passes(self) -> None:
        schema.validate_field_declarations(
            {"due": "2026-08-01"}, {"due": "date"}
        )

    def test_unknown_format_errors_listing_the_menu(self) -> None:
        with pytest.raises(SchemaViolation, match="formats: .*date.*text"):
            schema.validate_field_declarations(
                {"due": "2026-08-01"}, {"due": "datetime"}
            )

    def test_declared_key_missing_from_fields_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="no value"):
            schema.validate_field_declarations({}, {"due": "date"})

    def test_format_matching_is_case_and_space_insensitive(self) -> None:
        schema.validate_field_declarations({"due": "x"}, {"due": " Date "})


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
