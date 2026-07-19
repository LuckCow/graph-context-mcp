"""Automation-rule domain logic (WP31, ADR 039): parsing + matching."""

import pytest

from graph_context.domain import rules
from graph_context.errors import SchemaViolation


def fields(**overrides: str) -> dict[str, str]:
    """A valid stamp-completion rule's fields, overridable per test."""
    base = {
        rules.FIELD_TARGET_TYPE: "Task",
        rules.FIELD_WATCH_PROPERTY: "Done",
        rules.FIELD_CONDITION: "Changed to true",
        rules.FIELD_ACTION: "Set property to now",
        rules.FIELD_ACTION_PROPERTY: "Completion date",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v}


class TestNormalizeChoice:
    def test_seeded_select_options_round_trip_to_canonical_tokens(self) -> None:
        assert rules.normalize_choice("Changed to true") == rules.CONDITION_CHANGED_TO_TRUE
        assert rules.normalize_choice("Set property to now") == rules.ACTION_SET_NOW

    def test_hyphens_underscores_and_case_fold_away(self) -> None:
        assert rules.normalize_choice("changed-to-TRUE") == "changed to true"
        assert rules.normalize_choice("  set_property_value ") == "set property value"

    def test_empty_stays_empty(self) -> None:
        assert rules.normalize_choice("") == ""


class TestIsPaused:
    def test_only_explicit_off_words_pause(self) -> None:
        for word in ("Paused", "disabled", " OFF ", "cancelled"):
            assert rules.is_paused(word)

    def test_empty_unknown_active_and_error_all_stay_scanned(self) -> None:
        # Lenient like scheduling.is_active -- and Error MUST stay
        # scanned or a broken rule could never self-heal.
        for word in ("", "Active", "Error", "whatever"):
            assert not rules.is_paused(word)


class TestIsUnconfigured:
    def test_blank_target_and_watch_is_a_template(self) -> None:
        assert rules.is_unconfigured({})
        assert rules.is_unconfigured({rules.FIELD_ACTION: "Set property to now"})

    def test_either_half_configured_is_not(self) -> None:
        assert not rules.is_unconfigured({rules.FIELD_TARGET_TYPE: "Task"})
        assert not rules.is_unconfigured({rules.FIELD_WATCH_PROPERTY: "Done"})


class TestParseRuleFields:
    def test_a_full_stamp_completion_rule_parses(self) -> None:
        config = rules.parse_rule_fields(fields())
        assert config == rules.RuleConfig(
            target_type="Task",
            watch_property="Done",
            condition=rules.CONDITION_CHANGED_TO_TRUE,
            action=rules.ACTION_SET_NOW,
            action_property="Completion date",
            action_value="",
        )

    def test_missing_target_type_errors_with_guidance(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_TARGET_TYPE: ""}))
        assert "Rule target type" in str(err.value)

    def test_missing_watch_property_errors_with_guidance(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_WATCH_PROPERTY: ""}))
        assert "Rule watch property" in str(err.value)

    def test_unknown_condition_echoes_the_allowed_words(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_CONDITION: "on tuesdays"}))
        message = str(err.value)
        assert "'on tuesdays'" in message
        for condition in rules.CONDITIONS:
            assert condition in message

    def test_unknown_action_echoes_the_allowed_words(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_ACTION: "explode"}))
        message = str(err.value)
        for action in rules.ACTIONS:
            assert action in message

    def test_missing_condition_errors_for_ordinary_actions(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_CONDITION: ""}))
        assert "Rule condition" in str(err.value)

    def test_missing_action_property_errors_for_set_actions(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_ACTION_PROPERTY: ""}))
        assert "Rule action property" in str(err.value)

    def test_set_value_requires_a_value(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{rules.FIELD_ACTION: "Set property value"}))
        assert "Rule action value" in str(err.value)

    def test_uncheck_others_defaults_condition_and_property(self) -> None:
        config = rules.parse_rule_fields(fields(**{
            rules.FIELD_ACTION: "Uncheck others of type",
            rules.FIELD_CONDITION: "",
            rules.FIELD_ACTION_PROPERTY: "",
        }))
        assert config.condition == rules.CONDITION_CHANGED_TO_TRUE
        assert config.action_property == "Done"  # the watch property

    def test_uncheck_others_rejects_a_contradicting_condition(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            rules.parse_rule_fields(fields(**{
                rules.FIELD_ACTION: "Uncheck others of type",
                rules.FIELD_CONDITION: "Changed to false",
            }))
        assert rules.CONDITION_CHANGED_TO_TRUE in str(err.value)


class TestConditionMet:
    """The truth table. Absence is false: the Anytype adapter drops
    unticked checkboxes from fields ("" ≡ false), the fake stores an
    explicit "false" -- truthiness, not string identity, decides."""

    @pytest.mark.parametrize(("before", "after", "expected"), [
        ("", "true", True),
        ("false", "true", True),
        ("true", "true", False),  # already true: no transition
        ("", "", False),
        ("false", "", False),  # false either way: no transition
        ("true", "", False),  # that's a change to FALSE
    ])
    def test_changed_to_true(self, before: str, after: str, expected: bool) -> None:
        assert rules.condition_met(
            rules.CONDITION_CHANGED_TO_TRUE, before, after
        ) is expected

    @pytest.mark.parametrize(("before", "after", "expected"), [
        ("true", "", True),
        ("true", "false", True),
        ("", "false", False),  # false to false: no transition
        ("false", "", False),
        ("", "true", False),
    ])
    def test_changed_to_false(self, before: str, after: str, expected: bool) -> None:
        assert rules.condition_met(
            rules.CONDITION_CHANGED_TO_FALSE, before, after
        ) is expected

    @pytest.mark.parametrize(("before", "after", "expected"), [
        ("Todo", "Doing", True),
        ("Doing", "Doing", False),
        ("", "Todo", True),
        ("Todo", "", True),
        ("Todo", " Todo ", False),  # whitespace is not a change
    ])
    def test_changed_compares_values(self, before: str, after: str, expected: bool) -> None:
        assert rules.condition_met(rules.CONDITION_CHANGED, before, after) is expected
