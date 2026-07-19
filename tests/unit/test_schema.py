"""Schema: role resolution and node-creation invariants (open vocabulary)."""

import pytest

from graph_context.domain import models, schema
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


class TestPropertyDeclarations:
    def test_empty_declarations_are_a_noop(self) -> None:
        models.validate_property_declarations({"due"}, {})

    def test_valid_declaration_passes(self) -> None:
        models.validate_property_declarations(
            {"due"}, {"due": models.PropertyDeclaration("due", "date")}
        )

    def test_unknown_format_errors_listing_the_menu(self) -> None:
        with pytest.raises(SchemaViolation, match="formats: .*date.*text"):
            models.PropertyDeclaration("due", "datetime")

    def test_objects_format_is_creatable(self) -> None:
        # ADR 042: a relation is just a property whose format is objects.
        declaration = models.PropertyDeclaration("works_at", "objects")
        assert declaration.format == "objects"

    def test_declared_key_missing_from_properties_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="no value"):
            models.validate_property_declarations(
                set(), {"due": models.PropertyDeclaration("due", "date")}
            )

    def test_format_and_scope_normalize(self) -> None:
        declaration = models.PropertyDeclaration("due", " Date ", " TYPE ")
        assert declaration.format == "date"
        assert declaration.scope == "type"

    def test_unknown_scope_errors_naming_both_words(self) -> None:
        with pytest.raises(SchemaViolation, match="instance.*type"):
            models.PropertyDeclaration("due", "date", scope="global")

    def test_gc_prefix_is_reserved(self) -> None:
        with pytest.raises(SchemaViolation, match="gc_"):
            models.PropertyDeclaration("gc_due", "date")

    def test_display_name_derives_from_snake_case_key(self) -> None:
        assert (
            models.PropertyDeclaration("shift_active", "checkbox").display_name
            == "Shift Active"
        )

    def test_explicit_name_wins_over_derivation(self) -> None:
        declaration = models.PropertyDeclaration(
            "shift_active", "checkbox", name="On Shift"
        )
        assert declaration.display_name == "On Shift"


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


class TestValidateTypeName:
    def test_ordinary_display_name_passes(self) -> None:
        schema.validate_type_name("Faction")
        schema.validate_type_name("Meeting Notes")

    def test_empty_name_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="non-empty"):
            schema.validate_type_name("   ")

    def test_gc_prefix_is_reserved(self) -> None:
        with pytest.raises(SchemaViolation, match="reserved gc_"):
            schema.validate_type_name("gc_faction")
