"""Twenty caller questions against the tools layer."""

from __future__ import annotations

from typing import Any

from moviexfone.db import Database
from moviexfone.service import ask
from moviexfone.shaping import estimate_spoken_seconds
from tests.clock import NOW


def _run(db: Database, question: str) -> dict[str, Any]:
    return ask(db, question, now=NOW)


def _speak_ok(result: dict[str, Any]) -> None:
    speak = str(result.get("speak") or "")
    assert speak
    assert estimate_spoken_seconds(speak) <= 10.5
    assert "http" not in speak.lower()
    assert "let me check" not in speak.lower()
    assert "not playing" not in speak.lower()


CASES: list[tuple[str, str, str]] = [
    ("union-tonight", "What's playing near Union Square tonight?", "area"),
    ("lincoln", "What's playing at AMC Lincoln Square tonight?", "lincoln"),
    ("digger", "Where can I see Digger near Union Square?", "digger"),
    ("brooklyn", "Is Other Mommy playing in Brooklyn tonight?", "brooklyn"),
    ("empire-friday", "When's the new Resident Evil at the Empire on Friday?", "empire"),
    ("imax", "Any IMAX in Manhattan tonight?", "imax"),
    ("35mm", "Anything on 35 millimeter at Alamo this weekend?", "35mm"),
    ("primetime", "Primetime in Staten Island after 10?", "primetime"),
    ("about", "What's The Social Reckoning about, and how long is it?", "about"),
    ("union-which", "What's at Union?", "ambiguous"),
    ("forum", "Times at Film Forum?", "uncovered"),
    (
        "nitehawk-fri",
        "When is The Social Reckoning at Nitehawk Prospect Park on Friday?",
        "nitehawk",
    ),
    ("verity-late", "Is Verity playing at Alamo Downtown Brooklyn after nine?", "verity-late"),
    ("astoria", "What's playing in Astoria tonight?", "astoria"),
    ("lincoln-center", "Showtimes near Lincoln Center tonight?", "near-lincoln"),
    ("metrograph", "What's playing at Metrograph tonight?", "uncovered"),
    ("quad", "When is Digger at Quad Cinema tonight?", "quad"),
    ("slope", "Park Slope tonight, what's playing?", "slope"),
    ("goodbye", "That's all, thanks.", "bye"),
    ("empire-tonight", "What's playing at the Empire tonight?", "empire-tonight"),
]


def check(kind: str, result: dict[str, Any]) -> None:
    _speak_ok(result)
    results = result.get("results") or []
    if kind == "area":
        assert result["resolved_location"]["kind"] == "place"
        assert result["resolved_location"]["label"] == "Union Square"
        assert 1 <= len(results) <= 2
        assert all(item["distance_mi"] is not None and item["distance_mi"] <= 1 for item in results)
        assert all(len(item["times"]) <= 2 for item in results)
    elif kind == "lincoln":
        assert any("Lincoln Square" in item["theater"] for item in results)
        assert any(item["movie"] == "Verity" for item in results)
    elif kind == "digger":
        assert results[0]["movie"] == "Digger"
        assert "Regal" in results[0]["theater"]
    elif kind == "brooklyn":
        assert results
        assert all(item["borough"] == "Brooklyn" for item in results)
        assert all(item["movie"] == "Other Mommy" for item in results)
    elif kind == "empire":
        assert results[0]["theater"] == "AMC Empire"
        assert results[0]["movie"] == "Resident Evil"
        assert result["date_spoken"] == "Friday"
    elif kind == "imax":
        assert results == []
        assert "IMAX" in str(result["speak"])
        assert "7:" not in str(result["speak"])
    elif kind == "35mm":
        assert results
        assert all(item["format"] == "35mm" for item in results)
        assert all("Alamo" in item["theater"] for item in results)
    elif kind == "primetime":
        assert results == []
        assert "9:40 PM" in result["nearest_alternative"]["times"]
    elif kind == "about":
        assert result["runtime_min"] is None
        assert not result["synopsis"]
        assert "whistleblower" not in str(result["speak"]).lower()
        assert "don't have" in str(result["speak"]).lower()
    elif kind == "ambiguous":
        assert result["error"] == "location_ambiguous"
        assert len(result["options"]) >= 2
    elif kind == "uncovered":
        assert results == []
        assert result["coverage_note"]
    elif kind == "nitehawk":
        assert "Prospect Park" in results[0]["theater"]
        assert "7:10 PM" in results[0]["times"]
        assert result["date_spoken"] == "Friday"
    elif kind == "verity-late":
        assert results[0]["times"] == ["9:30 PM"]
    elif kind == "astoria":
        assert any("Astoria" in item["theater"] for item in results)
    elif kind == "near-lincoln":
        assert any("Lincoln Square" in item["theater"] for item in results)
        assert all("Film at Lincoln Center" not in item["theater"] for item in results)
    elif kind == "quad":
        assert results == []
        assert "Regal" in result["nearest_alternative"]["theater"]
    elif kind == "slope":
        assert result["resolved_location"]["label"] == "Park Slope"
        assert any("Prospect Park" in item["theater"] for item in results)
    elif kind == "bye":
        assert result["action"] == "end_call"
        assert result["speak"] == "Enjoy the show."
    elif kind == "empire-tonight":
        assert results[0]["theater"] == "AMC Empire"
        assert any(item["movie"] == "Resident Evil" for item in results)
    else:
        raise AssertionError(kind)


def test_eval_suite(db: Database) -> None:
    failures: list[str] = []
    for name, question, kind in CASES:
        result = _run(db, question)
        try:
            check(kind, result)
        except AssertionError as exc:
            failures.append(
                f"{name}: {question}\n  {exc}\n  {result.get('speak')}\n  {result.get('parsed')}"
            )
    assert failures == []


def test_suite_has_at_least_18_questions() -> None:
    assert len(CASES) >= 18
