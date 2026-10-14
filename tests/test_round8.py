"""Regressions from the eighth live test."""

from __future__ import annotations

from datetime import datetime, time

from moviexfone.catalog import _apply, _ensure_movie, load_places, refresh_live, seed
from moviexfone.db import Database
from moviexfone.geo import haversine_miles
from moviexfone.locations import resolve_location
from moviexfone.paths import data_dir
from moviexfone.query import search_showtimes
from moviexfone.sources.parse import IncomingShow
from moviexfone.timeutil import NY, spoken_time

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def test_failed_first_party_fetch_keeps_titles(tmp_path: object, monkeypatch: object) -> None:
    from pathlib import Path

    db = Database(Path(str(tmp_path)) / "keep.db")
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    _ensure_movie(db, "Home Invasion 1")
    _apply(
        db,
        [
            IncomingShow(
                theater_key="nitehawk-cinema-williamsburg",
                movie_title="Home Invasion 1",
                starts_at=datetime(2026, 10, 17, 11, 15, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://nitehawkcinema.com/williamsburg",
                source="nitehawk_jsonld",
            )
        ],
        mode="insert",
        now=NOW,
    )
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    calls = {"alamo": 0, "nitehawk": 0}

    def fail_alamo() -> list[IncomingShow]:
        calls["alamo"] += 1
        raise TimeoutError("timed out")

    def fail_nitehawk() -> list[IncomingShow]:
        calls["nitehawk"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr("moviexfone.sources.fetch.fetch_alamo", fail_alamo)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.fetch.fetch_nitehawk", fail_nitehawk)  # type: ignore[attr-defined]
    report = refresh_live(db, now=NOW, attempts=2, retry_sleep=0)
    assert calls == {"alamo": 2, "nitehawk": 2}
    assert report["alamo_error"] == "TimeoutError"
    assert report["nitehawk_error"] == "TimeoutError"
    title = db.query_one("SELECT title FROM movie WHERE title = ?", ("Home Invasion 1",))
    assert title is not None
    shows = db.query(
        "SELECT starts_at FROM showtime WHERE source = 'nitehawk_jsonld' AND movie_id LIKE 'movie:home-invasion-1%'"
    )
    assert shows
    found = search_showtimes(
        db,
        location="Nitehawk Williamsburg",
        movie="Home Invasion 1",
        dates=["2026-10-17"],
        now=NOW,
        date_spoken="Saturday",
    )
    assert "don't have that title" not in found["speak"].lower()
    assert "eleven fifteen" in found["speak"]
    db.close()


def test_brooklyn_heights_and_dumbo_stay_in_brooklyn(db: Database) -> None:
    def manhattan_point(_query: str) -> dict[str, object]:
        return {"lat": 40.7003, "lng": -73.9925, "label": "BROOKLYN HEIGHTS"}

    places = load_places()
    heights = resolve_location("Brooklyn Heights", [], places, geocode=manhattan_point)
    dumbo = resolve_location("DUMBO", [], places, geocode=manhattan_point)
    assert heights.borough == "Brooklyn"
    assert heights.label == "Brooklyn Heights"
    assert dumbo.borough == "Brooklyn"
    assert dumbo.label == "DUMBO"
    assert "Manhattan" not in heights.label


def test_empty_mile_widens_before_giving_up(db: Database) -> None:
    places = load_places()
    heights = resolve_location("Brooklyn Heights", [], places)
    assert heights.lat is not None and heights.lng is not None
    miles = haversine_miles(heights.lat, heights.lng, 40.6865, -73.9777)
    assert 1.0 < miles <= 2.0
    _apply(
        db,
        [
            IncomingShow(
                theater_key="bam-rose-cinemas",
                movie_title="Edie Arnold Is a Loser",
                starts_at=datetime(2026, 10, 14, 19, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://www.bam.org",
                source="nitehawk_jsonld",
            )
        ],
        mode="insert",
        now=NOW,
    )
    result = search_showtimes(
        db,
        location="Brooklyn Heights",
        movie="Edie Arnold Is a Loser",
        dates=["2026-10-14"],
        now=NOW,
        date_spoken="tomorrow",
    )
    assert "BAM" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()


def test_nearest_day_lists_times_without_a_stray_period(db: Database) -> None:
    _ensure_movie(db, "Home Invasion 2")
    _apply(
        db,
        [
            IncomingShow(
                theater_key="nitehawk-cinema-prospect-park",
                movie_title="Home Invasion 2",
                starts_at=datetime(2026, 10, 17, 14, 45, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://nitehawkcinema.com/prospectpark",
                source="nitehawk_jsonld",
            ),
            IncomingShow(
                theater_key="nitehawk-cinema-prospect-park",
                movie_title="Home Invasion 2",
                starts_at=datetime(2026, 10, 17, 16, 15, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://nitehawkcinema.com/prospectpark",
                source="nitehawk_jsonld",
            ),
            IncomingShow(
                theater_key="nitehawk-cinema-prospect-park",
                movie_title="Home Invasion 2",
                starts_at=datetime(2026, 10, 17, 19, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://nitehawkcinema.com/prospectpark",
                source="nitehawk_jsonld",
            ),
        ],
        mode="insert",
        now=NOW,
    )
    result = search_showtimes(
        db,
        location="Nitehawk Prospect Park",
        movie="Home Invasion 2",
        dates=["2026-10-18"],
        now=NOW,
        date_spoken="Sunday",
    )
    speak = result["speak"]
    assert ". at " not in speak
    assert spoken_time(time(14, 45)) in speak
    assert spoken_time(time(16, 15)) in speak
    assert spoken_time(time(19, 0)) in speak
    assert "Nitehawk Prospect Park" in speak


def test_late_night_with_no_title_names_tomorrow(db: Database) -> None:
    db.execute("DELETE FROM showtime")
    _apply(
        db,
        [
            IncomingShow(
                theater_key="bam-rose-cinemas",
                movie_title="Verity",
                starts_at=datetime(2026, 10, 14, 14, 45, tzinfo=NY),
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
        dates=["2026-10-13"],
        now=datetime(2026, 10, 13, 23, 30, tzinfo=NY),
        date_spoken="today",
    )
    assert "Nothing else tonight" in result["speak"]
    assert "Tomorrow" in result["speak"]
    assert "Verity" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()
    assert "Manhattan" not in result["speak"]
