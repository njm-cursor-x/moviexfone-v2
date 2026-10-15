from __future__ import annotations

from datetime import date, datetime, time

from moviexfone.timeutil import (
    NY,
    business_date,
    parse_after_phrase,
    parse_clock,
    parse_date_token,
    show_matches_after,
    spoken_time,
    upcoming_weekend,
)

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=NY)


def test_business_date_rolls_before_3am() -> None:
    late = datetime(2026, 10, 14, 1, 30, tzinfo=NY)
    assert business_date(late) == date(2026, 10, 13)
    assert business_date(NOW) == date(2026, 10, 13)


def test_parse_clock_and_reject_junk() -> None:
    assert parse_clock("17:00") == time(17, 0)
    assert parse_clock("24:00") is None
    assert parse_clock("nope") is None


def test_friday_from_tuesday() -> None:
    assert parse_date_token("friday", NOW.date()) == date(2026, 10, 16)
    assert parse_date_token("2026-10-17", NOW.date()) == date(2026, 10, 17)


def test_weekend_from_tuesday_is_upcoming() -> None:
    assert upcoming_weekend(NOW.date()) == [
        date(2026, 10, 16),
        date(2026, 10, 17),
        date(2026, 10, 18),
    ]


def test_weekend_on_saturday_skips_friday() -> None:
    assert upcoming_weekend(date(2026, 10, 17)) == [date(2026, 10, 17), date(2026, 10, 18)]


def test_after_phrases_are_evening() -> None:
    assert parse_after_phrase("after 10") == time(22, 0)
    assert parse_after_phrase("after nine") == time(21, 0)
    assert parse_after_phrase("after 9:30") == time(21, 30)
    assert parse_after_phrase("after 7 am") == time(7, 0)


def test_late_show_counts_as_after_an_evening_cutoff() -> None:
    assert show_matches_after(time(0, 30), time(22, 0)) is True
    assert show_matches_after(time(21, 40), time(22, 0)) is False
    assert show_matches_after(time(19, 0), time(17, 0)) is True


def test_spoken_clock() -> None:
    assert spoken_time(time(19, 0)) == "seven"
    assert spoken_time(time(19, 35)) == "seven thirty-five"
    assert spoken_time(time(22, 10)) == "ten ten"
    assert spoken_time(time(21, 40)) == "nine forty"
    assert spoken_time(time(19, 5)) == "seven oh five"
