"""Field-value coercion: the write-side normalization rule, in ONE place.

Both repository backends accept ``fields`` values as strings from the LLM
and must agree exactly on what parses, what errors, and how the value
reads back (the contract suite pins the parity). The accepted spellings,
the parse rules, and the LLM-facing error messages live here; the
adapters only translate the parsed value into their storage shape
(a wire ``property_entry`` for Anytype, a normalized read-back string
for the in-memory fake).
"""

from __future__ import annotations

from datetime import UTC, datetime

from graph_context.errors import GraphContextError

# Checkbox spellings: what a model may send, and which of those mean True.
CHECKBOX_VALUES = frozenset({"true", "false", "yes", "no", "1", "0"})
CHECKBOX_TRUTHY = frozenset({"true", "yes", "1"})


def parse_checkbox(field: str, value: str) -> bool:
    """The one checkbox-acceptance rule; the error echoes the fix."""
    lowered = value.strip().lower()
    if lowered not in CHECKBOX_VALUES:
        raise GraphContextError(
            f"field {field!r} is a checkbox property; got {value!r} "
            "(pass \"true\" or \"false\")"
        )
    return lowered in CHECKBOX_TRUTHY


def parse_number(field: str, value: str) -> float:
    """The one number-acceptance rule; the error echoes the fix."""
    try:
        return float(value)
    except ValueError:
        raise GraphContextError(
            f"field {field!r} is a number property; got {value!r} "
            "(pass a plain number, e.g. \"42\")"
        ) from None


def normalize_date(field: str, value: str) -> str:
    """The one date-acceptance rule; returns the stored read-back form.

    The live store (R2, WP31) accepts a bare date or an RFC 3339 stamp
    WITH a timezone, rejects naive timestamps, and reads every value
    back as the UTC instant (``YYYY-MM-DDTHH:MM:SSZ``) -- a bare date
    becomes midnight **UTC**. Beware the display shift that implies:
    midnight UTC renders a day early in zones west of Greenwich, so a
    zone-aware writer (the rule engine's ``set property to now``) sends
    local midnight with its explicit offset instead of a bare date.
    """
    raw = value.strip()
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise GraphContextError(
            f"field {field!r} is a date property; got {value!r} "
            "(pass \"YYYY-MM-DD\" or an RFC 3339 stamp with a UTC offset)"
        ) from None
    if moment.tzinfo is None:
        if raw != moment.date().isoformat():
            raise GraphContextError(
                f"field {field!r} is a date property; got the naive "
                f"timestamp {value!r} (the store rejects timestamps "
                "without a timezone -- pass \"YYYY-MM-DD\" or add a UTC "
                "offset, e.g. \"2026-07-19T00:00:00-04:00\")"
            )
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_multi_select(value: str) -> list[str]:
    """A multi_select value is a comma-separated list of option names."""
    return [part.strip() for part in value.split(",") if part.strip()]


def render_number(number: float) -> str:
    """Numbers read back untrailed: 1200.0 -> "1200", 1.5 -> "1.5"."""
    return str(int(number)) if number.is_integer() else str(number)
