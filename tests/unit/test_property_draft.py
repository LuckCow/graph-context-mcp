"""PropertyDraft (WP33, ADR 041): a malformed draft cannot be constructed."""

import pytest

from graph_context.domain.models import PropertyDraft, validate_property_drafts
from graph_context.errors import SchemaViolation


class TestPropertyDraftInvariants:
    def test_scalar_draft_constructs_and_renders(self) -> None:
        draft = PropertyDraft(name="Status", format="select",
                              options=("Open", "Done"))
        assert draft.render_hint() == "Status (select: Open, Done)"

    def test_empty_name_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="non-empty"):
            PropertyDraft(name=" ", format="text")

    def test_gc_prefix_is_reserved(self) -> None:
        with pytest.raises(SchemaViolation, match="reserved gc_"):
            PropertyDraft(name="gc_secret", format="text")

    def test_unknown_format_errors_listing_the_menu(self) -> None:
        with pytest.raises(SchemaViolation, match="formats: .*select.*text"):
            PropertyDraft(name="Status", format="datetime")

    def test_objects_format_redirects_to_relations(self) -> None:
        # Relations are edges (ADR 006); a schema proposal never mints one.
        with pytest.raises(SchemaViolation, match="relation"):
            PropertyDraft(name="Assignee", format="objects")

    def test_options_only_on_selects(self) -> None:
        with pytest.raises(SchemaViolation, match="options"):
            PropertyDraft(name="Motto", format="text", options=("x",))

    def test_empty_option_name_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="empty option"):
            PropertyDraft(name="Status", format="select", options=("Open", " "))


class TestValidatePropertyDrafts:
    def test_distinct_names_pass(self) -> None:
        validate_property_drafts((
            PropertyDraft(name="Motto", format="text"),
            PropertyDraft(name="Influence", format="number"),
        ))

    def test_duplicate_names_error_case_insensitively(self) -> None:
        with pytest.raises(SchemaViolation, match="twice"):
            validate_property_drafts((
                PropertyDraft(name="Motto", format="text"),
                PropertyDraft(name="motto", format="number"),
            ))
