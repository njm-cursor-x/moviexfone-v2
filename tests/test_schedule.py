"""Weekday population schedule and source/theater overwrite. No live HTTP."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from moviexfone.api.app import create_app
from moviexfone.catalog import populate_live, refresh_live, replace_showtimes
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.http import AllowedClient
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import (
    PopulationScheduler,
    RequestBudget,
    next_population_at,
    refresh_assemble,
)
from moviexfone.timeutil import NY
from tests.clock import NOW

BROOKLYN = "alamo-drafthouse-downtown-brooklyn"
LOWER = "alamo-drafthouse-lower-manhattan"
THURSDAY = datetime(2026, 10, 15, 6, 59, tzinfo=NY)


def _show(
    theater: str,
    title: str,
    starts: datetime,
    source: str,
) -> IncomingShow:
    return IncomingShow(
        theater_key=theater,
        movie_title=title,
        starts_at=starts,
        format_norm="standard",
        format_label=None,
        status="on_sale",
        ticket_url="https://drafthouse.com/nyc",
        source=source,
    )


def _starts(db: Database, *, source: str, theater: str) -> list[str]:
    rows = db.query(
        """SELECT movie_id, starts_at FROM showtime
           WHERE source = ? AND theater_id = ?
           ORDER BY movie_id, starts_at""",
        (source, theater),
    )
    return [f"{row['movie_id']} {row['starts_at']}" for row in rows]


def test_next_slot_is_monday_thursday_or_saturday() -> None:
    assert next_population_at(datetime(2026, 10, 12, 6, 0, tzinfo=NY)) == datetime(
        2026, 10, 12, 7, 0, tzinfo=NY
    )
    # Tuesday 07:00 is not a slot. The next one is Thursday.
    assert next_population_at(datetime(2026, 10, 13, 7, 0, tzinfo=NY)) == datetime(
        2026, 10, 15, 7, 0, tzinfo=NY
    )
    # Exactly 07:00 belongs to startup, not a second run.
    assert next_population_at(datetime(2026, 10, 15, 7, 0, tzinfo=NY)) == datetime(
        2026, 10, 17, 7, 0, tzinfo=NY
    )
    assert next_population_at(datetime(2026, 10, 17, 8, 0, tzinfo=NY)) == datetime(
        2026, 10, 19, 7, 0, tzinfo=NY
    )
    assert next_population_at(datetime(2026, 10, 18, 12, 0, tzinfo=NY)) == datetime(
        2026, 10, 19, 7, 0, tzinfo=NY
    )


def test_scheduler_fires_at_the_slot_and_calls_population(
    db: Database, monkeypatch: object
) -> None:
    clock = [THURSDAY]
    fired: list[datetime] = []

    def now() -> datetime:
        return clock[0]

    def wait(seconds: float) -> bool:
        if fired:
            return True
        clock[0] = clock[0] + timedelta(seconds=seconds)
        return False

    def fake(
        db: Database, *, now: datetime, attempts: int = 3, retry_sleep: float = 1.0
    ) -> dict[str, object]:
        fired.append(now)
        return {"alamo": 0, "nitehawk": 0, "assemble": 0}

    monkeypatch.setattr("moviexfone.catalog.populate_live", fake)  # type: ignore[attr-defined]
    scheduler = PopulationScheduler(db, now=now, wait=wait)
    scheduler.start()
    scheduler._thread.join(timeout=2)
    assert not scheduler._thread.is_alive()
    assert len(fired) == 1
    assert fired[0] == datetime(2026, 10, 15, 7, 0, tzinfo=NY)


def test_missed_slot_is_not_replayed(db: Database) -> None:
    fired: list[datetime] = []
    waited: list[float] = []

    def wait(seconds: float) -> bool:
        waited.append(seconds)
        return True

    def populate(db: Database, *, now: datetime) -> dict[str, object]:
        fired.append(now)
        return {}

    # Thursday 08:00. 07:00 already passed. Next start covers it, not this loop.
    scheduler = PopulationScheduler(
        db,
        populate=populate,
        now=lambda: datetime(2026, 10, 15, 8, 0, tzinfo=NY),
        wait=wait,
        max_sleep=10**9,
    )
    scheduler.start()
    scheduler._thread.join(timeout=2)
    assert fired == []
    assert waited
    assert waited[0] > 24 * 3600


def test_populate_live_calls_alamo_nitehawk_and_assemble(db: Database, monkeypatch: object) -> None:
    seen: list[str] = []

    def fake_live(
        db: Database, *, now: datetime, attempts: int = 3, retry_sleep: float = 1.0
    ) -> dict[str, object]:
        seen.append("live")
        return {"alamo": 1, "nitehawk": 1}

    def fake_assemble(db: Database, *, now: datetime, **kwargs: object) -> int:
        seen.append("assemble")
        return 4

    monkeypatch.setattr("moviexfone.catalog.refresh_live", fake_live)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.refresh.refresh_assemble", fake_assemble)  # type: ignore[attr-defined]
    report = populate_live(db, now=NOW)
    assert seen == ["live", "assemble"]
    assert report["assemble"] == 4
    assert report["alamo"] == 1


def test_refresh_overwrites_one_source_and_theater(db: Database, monkeypatch: object) -> None:
    replace_showtimes(
        db,
        [
            _show(BROOKLYN, "Verity", datetime(2026, 10, 14, 19, 0, tzinfo=NY), "alamo_json"),
            _show(BROOKLYN, "Verity", datetime(2026, 10, 14, 21, 0, tzinfo=NY), "alamo_json"),
            _show(BROOKLYN, "Verity", datetime(2026, 10, 14, 19, 0, tzinfo=NY), "alamo_json"),
            _show(LOWER, "Digger", datetime(2026, 10, 14, 18, 0, tzinfo=NY), "alamo_json"),
            _show("amc-empire-25", "Verity", datetime(2026, 10, 14, 20, 0, tzinfo=NY), "assemble"),
        ],
        now=NOW,
    )
    assert (
        _starts(db, source="alamo_json", theater=BROOKLYN).count(
            "movie:verity 2026-10-14T19:00:00-04:00"
        )
        == 1
    )

    def fetch_alamo() -> list[IncomingShow]:
        return [
            _show(BROOKLYN, "Verity", datetime(2026, 10, 14, 19, 0, tzinfo=NY), "alamo_json"),
            _show(BROOKLYN, "Digger", datetime(2026, 10, 14, 20, 0, tzinfo=NY), "alamo_json"),
        ]

    def fetch_nitehawk() -> list[IncomingShow]:
        raise TimeoutError("down")

    monkeypatch.setattr("moviexfone.sources.fetch.fetch_alamo", fetch_alamo)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.fetch.fetch_nitehawk", fetch_nitehawk)  # type: ignore[attr-defined]
    report = refresh_live(db, now=NOW, attempts=1, retry_sleep=0)
    assert report["alamo"] == 2
    assert report["nitehawk_error"] == "TimeoutError"
    assert _starts(db, source="alamo_json", theater=BROOKLYN) == [
        "movie:digger 2026-10-14T20:00:00-04:00",
        "movie:verity 2026-10-14T19:00:00-04:00",
    ]
    assert _starts(db, source="alamo_json", theater=LOWER) == [
        "movie:digger 2026-10-14T18:00:00-04:00"
    ]
    assert _starts(db, source="assemble", theater="amc-empire-25") == [
        "movie:verity 2026-10-14T20:00:00-04:00"
    ]


def test_failed_refresh_leaves_the_previous_rows(db: Database, monkeypatch: object) -> None:
    replace_showtimes(
        db,
        [_show(BROOKLYN, "Verity", datetime(2026, 10, 14, 19, 0, tzinfo=NY), "alamo_json")],
        now=NOW,
    )
    before = _starts(db, source="alamo_json", theater=BROOKLYN)

    def fail() -> list[IncomingShow]:
        raise TimeoutError("down")

    monkeypatch.setattr("moviexfone.sources.fetch.fetch_alamo", fail)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.fetch.fetch_nitehawk", fail)  # type: ignore[attr-defined]
    report = refresh_live(db, now=NOW, attempts=1, retry_sleep=0)
    assert report["alamo_error"] == "TimeoutError"
    assert report["nitehawk_error"] == "TimeoutError"
    assert _starts(db, source="alamo_json", theater=BROOKLYN) == before


def test_assemble_refresh_adds_and_overwrites_without_dropping_another_film(db: Database) -> None:
    digger_before = db.query_one(
        "SELECT COUNT(*) AS n FROM showtime WHERE source = 'assemble' AND movie_id = 'movie:digger'"
    )
    assert digger_before is not None and int(digger_before["n"]) > 0

    def client_for(starts: list[str]) -> AllowedClient:
        screenings = [
            {
                "venue": {"name": "Regal Union Square Stadium 14"},
                "starts_at": stamp,
                "availability": "on_sale",
                "url": "https://www.regmovies.com/showtimes/1",
            }
            for stamp in starts
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"film_id": "1283515", "screenings": screenings})

        return AllowedClient({"api.assemble.film"}, transport=httpx.MockTransport(handler))

    first = client_for(["2026-10-14T19:00", "2026-10-14T21:00", "2026-10-14T19:00"])
    refresh_assemble(
        db,
        now=NOW,
        budget=RequestBudget(),
        client=first,
        only_title="Verity",
        ttl=timedelta(seconds=1),
    )
    first.close()
    second = client_for(["2026-10-14T19:00", "2026-10-14T20:00"])
    refresh_assemble(
        db,
        now=NOW + timedelta(seconds=2),
        budget=RequestBudget(),
        client=second,
        only_title="Verity",
        ttl=timedelta(seconds=1),
    )
    second.close()
    times = _starts(db, source="assemble", theater="regal-union-square")
    verity = [item for item in times if item.startswith("movie:verity")]
    assert verity == [
        "movie:verity 2026-10-14T19:00:00-04:00",
        "movie:verity 2026-10-14T20:00:00-04:00",
    ]
    digger_after = db.query_one(
        "SELECT COUNT(*) AS n FROM showtime WHERE source = 'assemble' AND movie_id = 'movie:digger'"
    )
    assert digger_after is not None and int(digger_after["n"]) == int(digger_before["n"])


def test_entrypoint_defaults_to_a_live_fetch(monkeypatch: object, tmp_path: object) -> None:
    from pathlib import Path

    monkeypatch.chdir(Path(str(tmp_path)))  # type: ignore[attr-defined]
    monkeypatch.delenv("MOVIEXFONE_LIVE_DATA", raising=False)  # type: ignore[attr-defined]
    assert Settings.from_env().live_data is True
    monkeypatch.setenv("MOVIEXFONE_LIVE_DATA", "0")  # type: ignore[attr-defined]
    assert Settings.from_env().live_data is False


def test_server_startup_fetches_instead_of_loading_fixtures(
    settings: Settings, monkeypatch: object
) -> None:
    live_calls: list[str] = []
    assemble_calls: list[str] = []

    def fake_live(
        db: Database, *, now: datetime, attempts: int = 3, retry_sleep: float = 1.0
    ) -> dict[str, object]:
        live_calls.append("live")
        return {"alamo": 0, "nitehawk": 0}

    def fake_assemble(db: Database, *, now: datetime, **kwargs: object) -> int:
        assemble_calls.append("assemble")
        return 0

    def fail_closed() -> list[IncomingShow]:
        raise AssertionError("live network")

    monkeypatch.setattr("moviexfone.catalog.refresh_live", fake_live)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.refresh.refresh_assemble", fake_assemble)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.fetch.fetch_alamo", fail_closed)  # type: ignore[attr-defined]
    monkeypatch.setattr("moviexfone.sources.fetch.fetch_nitehawk", fail_closed)  # type: ignore[attr-defined]
    live = replace(settings, live_data=True)
    with TestClient(create_app(live)) as client:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not assemble_calls:
            time.sleep(0.02)
        health = client.get("/health").json()
        data = client.get("/health/data").json()
        assert health["live_data"] is True
        assert live_calls == ["live"]
        assert assemble_calls
        assert data["sources"] == []
        assert any(run["source"] == "registry" for run in data["recent_runs"])
        assert not any(run["source"] == "fixtures" for run in data["recent_runs"])
        assert client.app.state.schedule is not None
