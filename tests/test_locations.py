from __future__ import annotations

from moviexfone.db import Database
from moviexfone.geo import borough_of
from moviexfone.locations import resolve_location
from moviexfone.query import _places, _theaters


def test_every_geocoded_theater_sits_in_its_borough(db: Database) -> None:
    misses = []
    for row in db.query("SELECT name, borough, lat, lng FROM theater WHERE lat IS NOT NULL"):
        found = borough_of(float(row["lat"]), float(row["lng"]))
        if found != row["borough"]:
            misses.append((row["name"], row["borough"], found, row["lat"], row["lng"]))
    assert misses == []


def test_brooklyn_does_not_include_times_square() -> None:
    assert borough_of(40.7580, -73.9855) == "Manhattan"
    assert borough_of(40.6906, -73.9825) == "Brooklyn"
    assert borough_of(40.7568, -73.9245) == "Queens"
    assert borough_of(40.8265, -73.9255) == "Bronx"
    assert borough_of(40.5665, -74.1105) == "Staten Island"


def test_park_slope_does_not_call_geosearch(db: Database) -> None:
    called: list[str] = []

    def geocode(text: str) -> dict[str, object] | None:
        called.append(text)
        return None

    hit = resolve_location("park slope", _theaters(db), _places(), geocode=geocode)
    assert called == []
    assert hit.kind == "place"
    assert hit.label == "Park Slope"
    assert hit.borough == "Brooklyn"


def test_union_is_ambiguous_and_skips_geosearch(db: Database) -> None:
    called: list[str] = []
    hit = resolve_location(
        "union",
        _theaters(db),
        _places(),
        geocode=lambda text: called.append(text) or None,
    )
    assert called == []
    assert hit.kind == "ambiguous"
    labels = " ".join(hit.options).lower()
    assert "manhattan" in labels or "union square" in labels
    assert "brooklyn" in labels or "union street" in labels


def test_empire_is_one_theater(db: Database) -> None:
    hit = resolve_location("the empire", _theaters(db), _places())
    assert hit.kind == "theater"
    assert "Empire" in hit.label


def test_bare_alamo_is_the_chain(db: Database) -> None:
    hit = resolve_location("alamo", _theaters(db), _places())
    assert hit.kind == "chain"
    assert len(hit.theater_ids) == 3


def test_film_forum_is_uncovered(db: Database) -> None:
    hit = resolve_location("Film Forum", _theaters(db), _places())
    assert hit.kind == "theater"
    assert hit.covered is False
    assert hit.website and "filmforum.org" in hit.website


def test_url_looking_location_is_not_fetched(db: Database) -> None:
    called: list[str] = []
    hit = resolve_location(
        "http://169.254.169.254/latest",
        _theaters(db),
        _places(),
        geocode=lambda text: called.append(text) or None,
    )
    assert called == []
    assert hit.kind == "not_found"
