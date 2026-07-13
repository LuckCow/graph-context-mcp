"""Scheduled-event timing rules (WP18, ADR 027): parse, next_fire, due_at."""

from datetime import datetime

import pytest

from graph_context.domain.scheduling import (
    Cron,
    OneShot,
    due_at,
    is_disabled,
    next_fire,
    parse_schedule,
)
from graph_context.errors import SchemaViolation


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class TestParseOneShot:
    def test_iso_datetime_parses_as_one_shot(self) -> None:
        schedule = parse_schedule("2027-04-08T09:00")
        assert isinstance(schedule, OneShot)
        assert schedule.at == at("2027-04-08T09:00")

    def test_date_only_means_midnight(self) -> None:
        schedule = parse_schedule("2027-04-08")
        assert isinstance(schedule, OneShot)
        assert schedule.at == at("2027-04-08T00:00")

    def test_utc_offset_is_rejected_with_guidance(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            parse_schedule("2027-04-08T09:00+02:00")
        assert "LOCAL" in str(err.value)
        assert "2027-04-08T09:00" in str(err.value)  # the corrected example

    def test_garbage_error_teaches_both_formats(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            parse_schedule("next tuesday")
        message = str(err.value)
        assert "ISO" in message and "cron" in message

    def test_empty_and_off_are_rejected_by_parse(self) -> None:
        for raw in ("", "  ", "off", "OFF"):
            assert is_disabled(raw)
            with pytest.raises(SchemaViolation):
                parse_schedule(raw)


class TestParseCron:
    def test_five_fields_parse_as_cron(self) -> None:
        schedule = parse_schedule("0 9 * * 1")
        assert isinstance(schedule, Cron)
        assert schedule.minutes == frozenset({0})
        assert schedule.hours == frozenset({9})
        assert schedule.weekdays == frozenset({1})
        assert not schedule.day_restricted
        assert schedule.weekday_restricted

    def test_ranges_steps_and_lists_expand(self) -> None:
        schedule = parse_schedule("*/15 9-17 1,15 * 1-5")
        assert isinstance(schedule, Cron)
        assert schedule.minutes == frozenset({0, 15, 30, 45})
        assert schedule.hours == frozenset(range(9, 18))
        assert schedule.days == frozenset({1, 15})
        assert schedule.weekdays == frozenset({1, 2, 3, 4, 5})

    def test_weekday_seven_is_sunday(self) -> None:
        schedule = parse_schedule("0 0 * * 7")
        assert isinstance(schedule, Cron)
        assert schedule.weekdays == frozenset({0})

    def test_out_of_range_error_names_field_and_bounds(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            parse_schedule("0 9 * * 9")
        message = str(err.value)
        assert "weekday" in message and "0-7" in message

    def test_bad_token_error_echoes_the_syntax(self) -> None:
        with pytest.raises(SchemaViolation) as err:
            parse_schedule("0 9 * * mon")
        assert "minute hour day month weekday" in str(err.value)


class TestNextFire:
    def test_one_shot_fires_only_when_strictly_ahead(self) -> None:
        schedule = parse_schedule("2027-04-08T09:00")
        assert next_fire(schedule, at("2027-04-01T00:00")) == at("2027-04-08T09:00")
        assert next_fire(schedule, at("2027-04-08T09:00")) is None

    def test_weekly_cron_finds_the_next_monday(self) -> None:
        # 2026-07-12 is a Sunday.
        schedule = parse_schedule("0 9 * * 1")
        assert next_fire(schedule, at("2026-07-12T16:00")) == at("2026-07-13T09:00")

    def test_same_day_later_time_stays_on_the_day(self) -> None:
        schedule = parse_schedule("30 18 * * *")
        assert next_fire(schedule, at("2026-07-12T09:00")) == at("2026-07-12T18:30")

    def test_next_fire_is_strictly_after_the_anchor(self) -> None:
        schedule = parse_schedule("30 18 * * *")
        assert next_fire(schedule, at("2026-07-12T18:30")) == at("2026-07-13T18:30")

    def test_month_and_day_fields_roll_the_year(self) -> None:
        schedule = parse_schedule("0 9 8 4 *")  # April 8th, 09:00
        assert next_fire(schedule, at("2026-07-12T00:00")) == at("2027-04-08T09:00")

    def test_vixie_or_rule_when_day_and_weekday_both_restricted(self) -> None:
        # "the 15th OR a Monday", whichever comes first.
        schedule = parse_schedule("0 9 15 * 1")
        assert next_fire(schedule, at("2026-07-12T00:00")) == at("2026-07-13T09:00")
        # From the Monday itself the 15th (Wednesday) is next.
        assert next_fire(schedule, at("2026-07-13T09:00")) == at("2026-07-15T09:00")

    def test_impossible_date_reports_never(self) -> None:
        schedule = parse_schedule("0 9 30 2 *")  # Feb 30th
        assert next_fire(schedule, at("2026-07-12T00:00")) is None

    def test_leap_day_is_found_across_years(self) -> None:
        schedule = parse_schedule("0 9 29 2 *")
        assert next_fire(schedule, at("2026-07-12T00:00")) == at("2028-02-29T09:00")


class TestDueAt:
    def test_one_shot_never_fired_is_due_at_its_time(self) -> None:
        schedule = parse_schedule("2027-04-08T09:00")
        assert due_at(schedule, last_fired=None) == at("2027-04-08T09:00")

    def test_one_shot_is_spent_after_firing(self) -> None:
        schedule = parse_schedule("2027-04-08T09:00")
        assert due_at(schedule, last_fired=at("2027-04-08T09:05")) is None

    def test_rescheduling_a_fired_one_shot_rearms_it(self) -> None:
        # The stamp predates the (edited) schedule time -> due again.
        schedule = parse_schedule("2028-01-01T09:00")
        assert due_at(schedule, last_fired=at("2027-04-08T09:05")) == at(
            "2028-01-01T09:00"
        )

    def test_unanchored_cron_is_never_due(self) -> None:
        # The caller must arm it first; a fresh weekly event must not
        # fire the moment it is created.
        schedule = parse_schedule("0 9 * * 1")
        assert due_at(schedule, last_fired=None) is None

    def test_anchored_cron_is_due_at_the_next_occurrence(self) -> None:
        schedule = parse_schedule("0 9 * * 1")
        assert due_at(schedule, last_fired=at("2026-07-12T16:00")) == at(
            "2026-07-13T09:00"
        )

    def test_downtime_collapses_missed_occurrences_into_one_due_moment(self) -> None:
        # Daily at 09:00, last fired ten days ago: one earliest due moment
        # (the day after the stamp) -- comparing it against "now" yields
        # exactly one catch-up fire, not ten.
        schedule = parse_schedule("0 9 * * *")
        assert due_at(schedule, last_fired=at("2026-07-02T09:00")) == at(
            "2026-07-03T09:00"
        )
