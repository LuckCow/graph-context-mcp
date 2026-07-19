"""Automation-rule matching logic (WP31, ADR 039).

An Automation Rule node describes one reactive automation: WATCH a
scalar property on objects of one type, and when its value TRANSITIONS
a certain way, run a built-in action. The engine detects transitions by
diffing successive index states — this module only answers the pure
questions: does this fields map parse into a rule, and does this
before/after pair satisfy this condition?

Vocabulary values arrive as human-facing select options ("Changed to
true") and are normalized to canonical word tokens for comparison; the
adapter's checkbox convention (an unticked box is ABSENT from
``Node.fields``) makes ``""`` and ``"false"`` equally falsy here.

This module is pure: no clocks, no I/O — the engine passes values in.
Error messages echo the allowed vocabulary (errors are prompts).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from graph_context.errors import SchemaViolation

# Storage vocabulary for Automation Rule nodes — the one home for these
# keys (the Anytype adapter's mapping aliases them, like scheduling's).
RULE_TYPE_KEY = "gc_rule"
FIELD_TARGET_TYPE = "gc_rule_target_type"
FIELD_WATCH_PROPERTY = "gc_rule_watch_property"
FIELD_CONDITION = "gc_rule_condition"
FIELD_ACTION = "gc_rule_action"
FIELD_ACTION_PROPERTY = "gc_rule_action_property"
FIELD_ACTION_VALUE = "gc_rule_action_value"
FIELD_STATUS = "gc_rule_status"
FIELD_LAST_FIRED = "gc_rule_last_fired"
FIELD_LAST_ERROR = "gc_rule_last_error"

# The lifecycle select (human-visible as "Rule status"). Active rules
# are scanned and fired; Paused is the human's off-switch; Error is
# ENGINE-owned and self-healing — written with gc_rule_last_error when a
# rule fails to parse/resolve, flipped back to Active (error cleared)
# once the config parses again. The engine never writes Paused.
STATUS_ACTIVE = "Active"
STATUS_PAUSED = "Paused"
STATUS_ERROR = "Error"
_PAUSED_STATUSES = frozenset({"paused", "disabled", "off", "cancelled"})

# Canonical condition/action tokens: lowercase words, single-spaced.
# ``normalize_choice`` folds the seeded select options ("Changed to
# true") and reasonable human spellings ("changed-to-true") onto these.
CONDITION_CHANGED_TO_TRUE = "changed to true"
CONDITION_CHANGED_TO_FALSE = "changed to false"
CONDITION_CHANGED = "changed"
CONDITIONS = (
    CONDITION_CHANGED_TO_TRUE,
    CONDITION_CHANGED_TO_FALSE,
    CONDITION_CHANGED,
)

ACTION_SET_NOW = "set property to now"
ACTION_SET_VALUE = "set property value"
ACTION_UNCHECK_OTHERS = "uncheck others of type"
ACTIONS = (ACTION_SET_NOW, ACTION_SET_VALUE, ACTION_UNCHECK_OTHERS)

_CONDITION_WORDS = ", ".join(repr(c) for c in CONDITIONS)
_ACTION_WORDS = ", ".join(repr(a) for a in ACTIONS)


def is_paused(raw_status: str) -> bool:
    """Whether a stored status means "leave this rule alone".

    Deliberately lenient, like scheduling's ``is_active``: empty (a
    human-created object), unknown words, Active, and Error all keep the
    rule scanned — Error must stay scanned or it could never self-heal.
    Only an explicit off-word pauses.
    """
    return raw_status.strip().lower() in _PAUSED_STATUSES


def normalize_choice(raw: str) -> str:
    """Fold a select option / human spelling onto its canonical token
    (lowercase, hyphens/underscores/whitespace runs → single spaces)."""
    return " ".join(re.split(r"[\s_\-]+", raw.strip().lower())).strip()


def is_unconfigured(fields: Mapping[str, str]) -> bool:
    """A rule with NEITHER a target type nor a watch property is an
    unconfigured template (the seeded explainer, a half-created object):
    skipped silently, never an error."""
    return (
        not fields.get(FIELD_TARGET_TYPE, "").strip()
        and not fields.get(FIELD_WATCH_PROPERTY, "").strip()
    )


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """One parsed rule. Type/property identifiers stay AS TYPED — the
    engine resolves them against the space's catalog; condition and
    action are canonical tokens."""

    target_type: str
    watch_property: str
    condition: str
    action: str
    action_property: str
    action_value: str


def parse_rule_fields(fields: Mapping[str, str]) -> RuleConfig:
    """Parse a rule node's fields, or raise a self-correcting error."""
    target_type = fields.get(FIELD_TARGET_TYPE, "").strip()
    if not target_type:
        raise SchemaViolation(
            "the rule needs a 'Rule target type': the object type it "
            "watches (e.g. 'Task')"
        )
    watch_property = fields.get(FIELD_WATCH_PROPERTY, "").strip()
    if not watch_property:
        raise SchemaViolation(
            "the rule needs a 'Rule watch property': the property whose "
            "changes trigger it (e.g. 'Done')"
        )
    action = normalize_choice(fields.get(FIELD_ACTION, ""))
    if not action:
        raise SchemaViolation(
            f"the rule needs a 'Rule action': one of {_ACTION_WORDS}"
        )
    if action not in ACTIONS:
        raise SchemaViolation(
            f"unknown rule action {fields.get(FIELD_ACTION, '')!r}; "
            f"use one of {_ACTION_WORDS}"
        )
    condition = normalize_choice(fields.get(FIELD_CONDITION, ""))
    if action == ACTION_UNCHECK_OTHERS:
        # Exclusivity only makes sense on a box being TICKED; an empty
        # condition is filled in, a contradicting one is rejected loudly
        # rather than silently overridden.
        if condition and condition != CONDITION_CHANGED_TO_TRUE:
            raise SchemaViolation(
                f"action {ACTION_UNCHECK_OTHERS!r} always fires on "
                f"{CONDITION_CHANGED_TO_TRUE!r}; leave 'Rule condition' "
                "empty or set it to match"
            )
        condition = CONDITION_CHANGED_TO_TRUE
    elif not condition:
        raise SchemaViolation(
            f"the rule needs a 'Rule condition': one of {_CONDITION_WORDS}"
        )
    elif condition not in CONDITIONS:
        raise SchemaViolation(
            f"unknown rule condition {fields.get(FIELD_CONDITION, '')!r}; "
            f"use one of {_CONDITION_WORDS}"
        )
    action_property = fields.get(FIELD_ACTION_PROPERTY, "").strip()
    if action == ACTION_UNCHECK_OTHERS:
        action_property = action_property or watch_property
    elif not action_property:
        raise SchemaViolation(
            f"action {action!r} needs a 'Rule action property': the "
            "property it writes (e.g. 'Completion date')"
        )
    action_value = fields.get(FIELD_ACTION_VALUE, "").strip()
    if action == ACTION_SET_VALUE and not action_value:
        raise SchemaViolation(
            f"action {ACTION_SET_VALUE!r} needs a 'Rule action value': "
            "the value it writes"
        )
    return RuleConfig(
        target_type=target_type,
        watch_property=watch_property,
        condition=condition,
        action=action,
        action_property=action_property,
        action_value=action_value,
    )


def condition_met(condition: str, before: str, after: str) -> bool:
    """Does the before→after transition satisfy the (canonical)
    condition? ``before``/``after`` are normalized field strings; a
    checkbox that was never ticked reads as ``""`` on the Anytype
    backend and ``"false"`` on the fake, so truthiness — not string
    identity — decides the to-true/to-false conditions."""
    if condition == CONDITION_CHANGED_TO_TRUE:
        return _truthy(after) and not _truthy(before)
    if condition == CONDITION_CHANGED_TO_FALSE:
        return _truthy(before) and not _truthy(after)
    return before.strip() != after.strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() == "true"
