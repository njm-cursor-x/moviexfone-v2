"""Regressions from the ninth live check."""

from __future__ import annotations

import time
from datetime import datetime

from moviexfone.catalog import _apply
from moviexfone.db import Database
from moviexfone.query import search_showtimes
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import RequestBudget
from moviexfone.timeutil import NY

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def _tomorrow_show() -> IncomingShow:
    return IncomingShow(
        theater_key="regal-union-square",
        movie_title="Verity",
        starts_at=datetime(2026, 10, 14, 11, 0, tzinfo=NY),
        format_norm="unknown",
        format_label=None,
        status="on_sale",
        ticket_url="https://www.regmovies.com/theatres/regal-union-square",
        source="assemble",
    )


def test_eleven_thirty_with_no_date_is_tonight(db: Database) -> None:
    db.execute("DELETE FROM showtime")
    _apply(db, [_tomorrow_show()], mode="insert", now=NOW)
    result = search_showtimes(
        db,
        location="Union Square",
        dates=["2026-10-13"],
        now=datetime(2026, 10, 13, 23, 30, tzinfo=NY),
        date_spoken="today",
    )
    assert "Nothing else tonight" in result["speak"]
    assert "Tomorrow" in result["speak"]
    assert "Verity" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()


def test_one_thirty_am_with_no_date_is_tonight(db: Database) -> None:
    db.execute("DELETE FROM showtime")
    _apply(db, [_tomorrow_show()], mode="insert", now=NOW)
    result = search_showtimes(
        db,
        location="Union Square",
        dates=["2026-10-13"],
        now=datetime(2026, 10, 14, 1, 30, tzinfo=NY),
        date_spoken="today",
    )
    assert "Nothing else tonight" in result["speak"]
    assert "Tomorrow" in result["speak"]
    assert "Verity" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()


def test_widened_search_says_farther_not_nearby(db: Database) -> None:
    db.execute("DELETE FROM showtime")
    _apply(
        db,
        [
            IncomingShow(
                theater_key="bam-rose-cinemas",
                movie_title="Verity",
                starts_at=datetime(2026, 10, 14, 19, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://www.bam.org",
                source="alamo_json",
            )
        ],
        mode="insert",
        now=NOW,
    )
    result = search_showtimes(
        db,
        location="Brooklyn Heights",
        dates=["2026-10-14"],
        now=NOW,
        date_spoken="tomorrow",
    )
    speak = result["speak"].lower()
    assert "a bit farther" in speak
    assert "about a mile" in speak
    assert "nearby" not in speak


def test_assemble_budget_survives_a_restart(db: Database) -> None:
    first = RequestBudget(limit=2, window=0.4)
    first.attach(db)
    first.wait()
    first.wait()
    restarted = RequestBudget(limit=2, window=0.4)
    restarted.attach(db)
    assert len(restarted._stamps) == 2
    began = time.time()
    restarted.wait()
    assert time.time() - began >= 0.2
    again = RequestBudget(limit=2, window=60)
    again.attach(db)
    assert again._stamps
    assert RequestBudget().limit == 50
