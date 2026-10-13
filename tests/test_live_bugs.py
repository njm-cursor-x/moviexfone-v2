"""Regressions from the 2026-10-13 live test."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from moviexfone.api.app import create_app
from moviexfone.catalog import _apply, normalize_venue, seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.http import AllowedClient
from moviexfone.query import get_movie_info, search_showtimes
from moviexfone.sources.fetch import ALL_CITIES, pick_geosearch
from moviexfone.sources.parse import IncomingShow, parse_alamo, parse_assemble, parse_nitehawk_html
from moviexfone.sources.refresh import RequestBudget, assemble_is_fresh, refresh_assemble
from moviexfone.timeutil import NY, resolve_when, spoken_time
from moviexfone.titles import resolve_title, score_title
from moviexfone.voice.bridge import _session_body
from moviexfone.voice.loop import ToolLoop
from tests.clock import NOW

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "data" / "fixtures"


def test_live_seed_skips_fixture_showtimes(tmp_path: Path) -> None:
    db = Database(tmp_path / "live.db")
    summary = seed(db, ROOT / "data", now=NOW, include_showtimes=False)
    assert summary["showtimes"] == 0
    assert summary["theaters"] > 60
    assert db.query_one("SELECT COUNT(*) AS n FROM showtime")["n"] == 0
    db.close()


def test_real_assemble_needs_the_film_title() -> None:
    payload = json.loads((FIX / "assemble_verity_new-york-ny.json").read_text(encoding="utf-8"))
    assert parse_assemble(payload) == []
    rows = parse_assemble(payload, title="Verity")
    assert rows
    assert {row.movie_title for row in rows} == {"Verity"}
    assert "Regal Union Square Stadium 14" in {row.theater_key for row in rows}


def test_venue_aliases_match_the_registry(db: Database) -> None:
    payload = json.loads((FIX / "assemble_verity_brooklyn-ny.json").read_text(encoding="utf-8"))
    stored = _apply(db, parse_assemble(payload, title="Verity"), mode="insert", now=NOW)
    assert stored >= 4
    found = db.query(
        """SELECT t.spoken_name FROM showtime s
           JOIN theater t ON t.theater_id = s.theater_id
           WHERE s.source = 'assemble' AND s.movie_id = 'movie:verity'"""
    )
    names = {row["spoken_name"] for row in found}
    assert {"Alpine Cinema", "Regal Sheepshead Bay", "Cobble Hill Cinemas", "Kent Theater"} <= names
    assert "union square" in normalize_venue("Regal Union Square Stadium 14")


def test_tool_rounds_reset_each_question() -> None:
    calls: list[str] = []

    def execute(name: str, args: dict[str, object]) -> dict[str, object]:
        calls.append(name)
        return {"speak": "one listing"}

    loop = ToolLoop(execute, send=_sink)

    async def turn() -> None:
        await loop.handle_server_event({"type": "input_audio_buffer.speech_started"})
        await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c",
                "name": "search_showtimes",
                "arguments": "{}",
            }
        )
        await loop.handle_server_event({"type": "response.done"})
        await loop.handle_server_event({"type": "response.done"})

    async def three() -> None:
        for _ in range(3):
            await turn()

    asyncio.run(three())
    assert calls == ["search_showtimes"] * 3


async def _sink(_message: dict[str, object]) -> None:
    return None


def test_nitehawk_suffix_is_one_movie() -> None:
    rows = parse_nitehawk_html(
        (FIX / "nitehawk_williamsburg_live.html").read_text(encoding="utf-8"),
        default_theater="Nitehawk Cinema Williamsburg",
    )
    assert {row.movie_title for row in rows} == {"Digger", "The Social Reckoning"}
    assert len(rows) == 2


def test_past_showtimes_are_dropped(db: Database) -> None:
    result = search_showtimes(
        db,
        location="Union Square",
        dates=["2026-10-13"],
        now=NOW + timedelta(hours=8),
        date_spoken="today",
    )
    assert result["results"] == []
    assert "don't have live times" in str(result["speak"])


def test_tomorrow_night_and_next_weekday() -> None:
    dates, spoken, unclear = resolve_when("tomorrow night", NOW)
    assert dates == ["2026-10-14"]
    assert spoken == "tomorrow"
    assert unclear is False
    nxt, _label, unclear = resolve_when("next Tuesday", NOW)
    assert nxt == ["2026-10-20"]
    assert unclear is False
    _dates, _spoken, unclear = resolve_when("someday soon", NOW)
    assert unclear is True


def test_after_midnight_tonight_keeps_the_late_show(db: Database) -> None:
    late = datetime(2026, 10, 14, 0, 30, tzinfo=NY)
    _apply(
        db,
        [
            IncomingShow(
                theater_key="Alamo Drafthouse Staten Island",
                movie_title="Primetime",
                starts_at=datetime(2026, 10, 14, 0, 45, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url=None,
                source="alamo_json",
            )
        ],
        mode="insert",
        now=late,
    )
    found = search_showtimes(
        db,
        location="Alamo Staten Island",
        movie="Primetime",
        dates=["2026-10-13"],
        now=late,
        date_spoken="tonight",
    )
    times = [stamp for row in found["results"] for stamp in row["times"]]
    assert "12:45 AM" in times


def test_year_and_typo_titles_collapse() -> None:
    catalog = [
        ("movie:resident-evil", "Resident Evil", ["resident evil"]),
        ("movie:pans", "Pan's Labyrinth 20th Anniversary", []),
    ]
    hit, _suggestions = resolve_title("Resident Evil (2026)", catalog)
    assert hit is not None and hit.movie_id == "movie:resident-evil"
    assert score_title("Pans Labrynth", "Pan's Labyrinth 20th Anniversary") >= 0.84


def test_voice_origin_is_rejected(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect(
            "/ws/voice", headers={"origin": "https://evil.example"}
        ) as socket:
            message = socket.receive_json()
            assert message["type"] == "error"
        denied = client.post(
            "/v1/demo/ask",
            json={"text": "What's playing near Union Square tonight?"},
            headers={"origin": "https://evil.example"},
        )
        assert denied.status_code == 403


def test_idle_timeout_is_not_a_nag(db: Database) -> None:
    assert _session_body(db)["turn_detection"]["idle_timeout_ms"] >= 60000


def test_empty_neighborhood_is_honest(db: Database) -> None:
    result = search_showtimes(
        db,
        location="Forest Hills",
        dates=["2026-12-01"],
        now=NOW,
        date_spoken="that day",
    )
    assert result["results"] == []
    assert "don't have live times" in str(result["speak"])
    assert "everything I have" not in str(result["speak"])


def test_builder_nulls_and_imax_case(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/tools/search_showtimes",
            headers={"X-MovieXFone-Secret": "test-secret"},
            json={"location": "Union Square", "format": None, "page": None, "movie": None},
        )
        assert response.status_code == 200
        imax = client.post(
            "/v1/tools/search_showtimes",
            headers={"X-MovieXFone-Secret": "test-secret"},
            json={"location": "Union Square", "format": "IMAX"},
        )
        assert imax.status_code == 200
        unclear = client.post(
            "/v1/tools/search_showtimes",
            headers={"X-MovieXFone-Secret": "test-secret"},
            json={"location": "Union Square", "date": "someday soon"},
        )
        assert unclear.json()["error"] == "date_unclear"


def test_morning_and_weekend_days(db: Database) -> None:
    from moviexfone.service import ask

    assert spoken_time(time(10, 0)) == "ten a.m."
    result = ask(db, "Anything on 35 millimeter at Alamo this weekend?", now=NOW)
    assert "Saturday" in str(result["speak"])


def test_placeholder_metadata_is_not_spoken(db: Database) -> None:
    info = get_movie_info(db, "The Social Reckoning")
    assert info["runtime_min"] is None
    assert "unrated" not in str(info["speak"]).lower()
    assert "whistleblower" not in str(info["speak"]).lower()


def test_not_on_sale_alamo_sessions_are_dropped() -> None:
    payload = json.loads((FIX / "alamo_live_sample.json").read_text(encoding="utf-8"))
    rows = parse_alamo(payload)
    assert rows
    assert all(row.status == "on_sale" for row in rows)


def test_geosearch_prefers_a_zip() -> None:
    payload = {
        "features": [
            {
                "geometry": {"coordinates": [-73.99, 40.71]},
                "properties": {
                    "name": "5 SOME STREET",
                    "housenumber": "5",
                    "borough": "Brooklyn",
                    "layer": "venue",
                },
            },
            {
                "geometry": {"coordinates": [-73.95, 40.71]},
                "properties": {"name": "11211", "borough": "Brooklyn", "layer": "postalcode"},
            },
        ]
    }
    picked = pick_geosearch(payload, "11211")
    assert picked is not None
    assert picked["label"] == "11211"


def test_zip_geocode_is_used(db: Database) -> None:
    def geocode(text: str) -> dict[str, object] | None:
        if text == "11211":
            return {"label": "11211", "lat": 40.712, "lng": -73.953, "borough": "Brooklyn"}
        return None

    result = search_showtimes(
        db,
        location="11211",
        dates=["2026-10-13"],
        now=NOW,
        date_spoken="today",
        geocode=geocode,
    )
    assert result["resolved_location"]["label"] == "11211"


def test_one_theater_borough_lists_more_than_one_movie(db: Database) -> None:
    _apply(
        db,
        [
            IncomingShow(
                theater_key="Alamo Drafthouse Staten Island",
                movie_title="Digger",
                starts_at=datetime(2026, 10, 13, 20, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url=None,
                source="alamo_json",
            )
        ],
        mode="insert",
        now=NOW,
    )
    result = search_showtimes(
        db,
        location="Staten Island",
        dates=["2026-10-13"],
        now=NOW,
        date_spoken="today",
    )
    titles = {row["movie"] for row in result["results"]}
    assert len(titles) >= 2


def test_assemble_cache_skips_a_fresh_film(tmp_path: Path) -> None:
    db = Database(tmp_path / "assemble.db")
    seed(db, ROOT / "data", now=NOW, include_showtimes=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "film_id": "1283515",
                "screenings": [
                    {
                        "venue": {"name": "Regal Union Square Stadium 14"},
                        "starts_at": "2026-10-13T19:00",
                        "availability": "on_sale",
                        "url": "https://www.regmovies.com/showtimes/1",
                    }
                ],
            },
        )

    client = AllowedClient({"api.assemble.film"}, transport=httpx.MockTransport(handler))
    count = refresh_assemble(db, now=NOW, budget=RequestBudget(), client=client)
    assert count >= 1
    assert len(calls) == len(ALL_CITIES)
    assert assemble_is_fresh(db, "movie:verity", now=NOW)
    before = len(calls)
    assert refresh_assemble(db, now=NOW, budget=RequestBudget(), client=client) == 0
    assert len(calls) == before
    client.close()
    db.close()


def test_docs_are_not_blocked_by_the_demo_csp(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        docs = client.get("/openapi.json")
        assert "Content-Security-Policy" not in docs.headers
        page = client.get("/")
        assert "script-src 'self'" in page.headers["content-security-policy"]


def test_health_data_reports_the_last_fetch(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        body = client.get("/health/data").json()
        assert body["by_source"]["fixtures"]["last_ok"] is True
