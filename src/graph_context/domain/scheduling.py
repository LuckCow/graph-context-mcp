"""Scheduled-event timing logic (WP18, ADR 027).

A Scheduled Event node carries a ``gc_schedule`` string in ONE of two
forms, auto-detected by shape:

* **one-shot** -- an ISO-8601 local date-time (``2027-04-08T09:00``);
  fires once, and again only if the schedule is edited to a later time.
* **cron** -- five whitespace-separated fields (``minute hour day month
  weekday``), the classic vixie subset: ``*``, numbers, ranges ``a-b``,
  steps ``*/n`` / ``a-b/n``, and comma lists. Weekday 0-7 with both 0
  and 7 meaning Sunday; when day AND weekday are both restricted, a date
  matching EITHER fires (the vixie OR rule).

All datetimes here are NAIVE and mean the server's local wall-clock time
-- cron lines have no timezone, and mixing aware/naive values is a bug
factory, so an offset in a one-shot value is rejected with guidance.
This module is pure: no clocks, no I/O -- callers pass ``now``/anchor
values in. Error messages echo the allowed syntax (errors are prompts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from graph_context.errors import SchemaViolation

# A cancelled event keeps its node (the store has no delete); its schedule
# is set to this marker. Empty means "never scheduled" and is treated the
# same. Matching is case-insensitive.
DISABLED_MARKER = "off"

# Storage vocabulary for Scheduled Event nodes -- the one home for these
# keys (the Anytype adapter's mapping aliases them, the way its
# REFLECTED_FIELD_FORMATS aliases schema.FIELD_FORMATS).
SCHEDULED_TYPE_KEY = "gc_scheduled_event"
FIELD_SCHEDULE = "gc_schedule"
FIELD_PROMPT = "gc_schedule_prompt"
FIELD_LAST_FIRED = "gc_last_fired"
FIELD_STATUS = "gc_schedule_status"
# The delivery target: which chat's session a fired event speaks into.
# Same property session nodes carry (mapping.PROP_SESSION_KEY, ADR 021).
FIELD_SESSION_KEY = "gc_session_key"

# The lifecycle select (human-visible as "Schedule status"): Pending
# events are scanned and fired; Completed/Cancelled ones are inert. The
# scheduler owns the transitions (Pending at creation, Completed when a
# one-shot fires, Cancelled on the tool's cancel); a human re-enables an
# event by flipping the status back to Pending in Anytype.
STATUS_PENDING = "Pending"
STATUS_COMPLETED = "Completed"
STATUS_CANCELLED = "Cancelled"
_INACTIVE_STATUSES = frozenset({"completed", "cancelled", "done", "off"})


def is_active(raw_status: str) -> bool:
    """Whether a stored status means "keep scanning this event".

    Deliberately lenient: empty (a human-created object) and unknown
    values read as pending -- people must not need our vocabulary for
    their reminder to work; only an explicit completion word deactivates.
    """
    return raw_status.strip().lower() not in _INACTIVE_STATUSES

_CRON_FORMAT = (
    "5 cron fields 'minute hour day month weekday' "
    "(e.g. '0 9 * * 1' = Mondays 09:00; ranges a-b, steps */n, lists a,b)"
)
_SCHEDULE_FORMAT = (
    "a schedule is either an ISO local date-time for a single fire "
    f"(e.g. '2027-04-08T09:00') or {_CRON_FORMAT}"
)

# Cron can express dates that never occur (e.g. Feb 30). The day scan
# gives up after four years -- long enough for every real rule (Feb 29
# included) -- and reports "never" as None.
_MAX_SCAN_DAYS = 4 * 366


@dataclass(frozen=True, slots=True)
class OneShot:
    """Fire once at a fixed local time."""

    at: datetime

    def describe(self) -> str:
        return f"once at {self.at.isoformat(sep=' ', timespec='minutes')}"


@dataclass(frozen=True, slots=True)
class Cron:
    """A recurring rule; field sets are pre-expanded for matching."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 == Sunday (cron convention)
    day_restricted: bool
    weekday_restricted: bool
    text: str  # the original five-field line, for display

    def describe(self) -> str:
        return f"repeating '{self.text}' (minute hour day month weekday)"


Schedule = OneShot | Cron


def is_disabled(raw: str) -> bool:
    """Whether a stored ``gc_schedule`` value means "do not fire"."""
    return raw.strip().lower() in {"", DISABLED_MARKER}


def parse_schedule(raw: str) -> Schedule:
    """Parse a ``gc_schedule`` string, or raise a self-correcting error."""
    text = raw.strip()
    if is_disabled(text):
        raise SchemaViolation(
            f"schedule {raw!r} is empty/disabled; {_SCHEDULE_FORMAT}"
        )
    fields = text.split()
    if len(fields) == 5:
        return _parse_cron(fields, text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise SchemaViolation(
            f"cannot parse schedule {text!r}: {_SCHEDULE_FORMAT}"
        ) from None
    if moment.tzinfo is not None:
        raise SchemaViolation(
            f"schedule {text!r} carries a UTC offset; schedules use the "
            "server's LOCAL wall-clock time -- resend without the offset "
            "(e.g. '2027-04-08T09:00')"
        )
    return OneShot(at=moment)


def due_at(schedule: Schedule, last_fired: datetime | None) -> datetime | None:
    """The earliest moment the schedule should fire (``None`` = never).

    ``last_fired`` is when it last actually fired. A one-shot is spent
    once it has fired at-or-after its time -- editing the schedule to a
    later time re-arms it. A cron rule needs an anchor: an un-anchored
    (never-fired) recurring event returns ``None``, and the caller must
    first ARM it by stamping ``last_fired`` = now, so a fresh "every
    Monday 09:00" waits for Monday instead of firing immediately.
    Comparing the result against the caller's ``now`` also yields
    catch-up-once semantics for downtime: however many occurrences were
    missed, there is one earliest due moment, hence one fire.
    """
    if isinstance(schedule, OneShot):
        if last_fired is not None and last_fired >= schedule.at:
            return None
        return schedule.at
    if last_fired is None:
        return None
    return next_fire(schedule, after=last_fired)


def next_fire(schedule: Schedule, after: datetime) -> datetime | None:
    """The first moment STRICTLY after ``after`` the schedule fires."""
    if isinstance(schedule, OneShot):
        return schedule.at if schedule.at > after else None
    # Cron matches whole minutes: start at the next minute boundary.
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for offset in range(_MAX_SCAN_DAYS):
        candidate = day + timedelta(days=offset)
        if not _day_matches(schedule, candidate):
            continue
        floor = start if candidate == start.date() else None
        moment = _first_time_on_day(schedule, candidate, floor)
        if moment is not None:
            return moment
    return None


# -- internals -------------------------------------------------------------


def _day_matches(cron: Cron, candidate: date) -> bool:
    if candidate.month not in cron.months:
        return False
    in_days = candidate.day in cron.days
    # Python: Monday == 0; cron: Sunday == 0.
    in_weekdays = (candidate.weekday() + 1) % 7 in cron.weekdays
    if cron.day_restricted and cron.weekday_restricted:
        return in_days or in_weekdays  # the vixie OR rule
    return in_days and in_weekdays


def _first_time_on_day(
    cron: Cron, day: date, floor: datetime | None
) -> datetime | None:
    """The day's earliest matching time at/after ``floor`` (``None`` = any)."""
    for hour in sorted(cron.hours):
        if floor is not None and hour < floor.hour:
            continue
        for minute in sorted(cron.minutes):
            if floor is not None and hour == floor.hour and minute < floor.minute:
                continue
            return datetime(day.year, day.month, day.day, hour, minute)
    return None


_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _parse_cron(fields: list[str], text: str) -> Cron:
    expanded: list[frozenset[int]] = []
    for field, name, (low, high) in zip(fields, _FIELD_NAMES, _FIELD_RANGES, strict=True):
        expanded.append(_parse_field(field, name, low, high))
    minutes, hours, days, months, weekdays = expanded
    if 7 in weekdays:  # 0 and 7 both mean Sunday
        weekdays = weekdays - {7} | {0}
    return Cron(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        day_restricted=fields[2] != "*",
        weekday_restricted=fields[4] != "*",
        text=text,
    )


def _parse_field(field: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        values.update(_parse_part(part, name, low, high))
    return frozenset(values)


def _parse_part(part: str, name: str, low: int, high: int) -> set[int]:
    error = SchemaViolation(
        f"bad cron {name} field {part!r}; {_CRON_FORMAT}"
    )
    step = 1
    if "/" in part:
        part, _, raw_step = part.partition("/")
        if not raw_step.isdigit() or int(raw_step) < 1:
            raise error
        step = int(raw_step)
    if part == "*":
        first, last = low, high
    elif "-" in part:
        raw_first, _, raw_last = part.partition("-")
        if not raw_first.isdigit() or not raw_last.isdigit():
            raise error
        first, last = int(raw_first), int(raw_last)
    elif part.isdigit():
        first = last = int(part)
    else:
        raise error
    if not (low <= first <= last <= high):
        raise SchemaViolation(
            f"cron {name} value {part!r} is out of range {low}-{high}; "
            f"{_CRON_FORMAT}"
        )
    return set(range(first, last + 1, step))
