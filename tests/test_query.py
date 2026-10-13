from __future__ import annotations

from tests.conftest import NOW

from moviexfone.db import Database
from moviexfone.service import ask


def _ask(db: Database, text: str) -> dict[str, object]:
    return ask(db, text, now=NOW)


def test_alamo_beats_wrong_assemble_time(db: Database) -> None:
    result = _ask(db, "When is Verity at Alamo Lower Manhattan today?")
    times = [item["times"] for item in result["results"]]  # type: ignore[index]
    flat = [stamp for group in times for stamp in group]
    assert "7:00 PM" in flat
    assert "3:00 PM" not in flat


def test_nitehawk_jsonld_beats_assemble_for_today(db: Database) -> None:
    result = _ask(db, "When is The Social Reckoning at Nitehawk Prospect Park tonight?")
    times = result["results"][0]["times"]  # type: ignore[index]
    assert times == ["7:30 PM"]


def test_nitehawk_friday_still_comes_from_assemble(db: Database) -> None:
    result = _ask(db, "When is The Social Reckoning at Nitehawk Prospect Park on Friday?")
    times = result["results"][0]["times"]  # type: ignore[index]
    assert "7:10 PM" in times
    assert "9:50 PM" in times
    assert result["results"][0]["ticket_url"]  # type: ignore[index]


def test_empty_result_offers_nearest_alternative(db: Database) -> None:
    result = _ask(db, "Primetime in Staten Island after 10?")
    assert result["results"] == []
    alt = result["nearest_alternative"]
    assert isinstance(alt, dict)
    assert "9:40 PM" in alt["times"]
    assert "Alamo" in alt["theater"]
    note = str(result["note"]).lower()
    assert "do not search again" in note
    assert "not playing" not in str(result["speak"]).lower()


def test_film_forum_is_uncovered(db: Database) -> None:
    result = _ask(db, "Times at Film Forum?")
    assert result["results"] == []
    assert "Film Forum" in str(result["coverage_note"])
    assert "http" not in str(result["speak"])


def test_brooklyn_polygon_excludes_manhattan(db: Database) -> None:
    result = _ask(db, "Is Other Mommy playing in Brooklyn tonight?")
    assert result["results"]
    assert all(item["borough"] == "Brooklyn" for item in result["results"])  # type: ignore[index]
