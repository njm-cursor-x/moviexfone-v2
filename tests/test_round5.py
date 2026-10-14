"""Regressions from the fifth live test."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, time, timedelta

from moviexfone.catalog import _apply, _ensure_movie, load_places, load_theater_refs, seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.localgeo import lookup_local, speakable_station
from moviexfone.locations import resolve_location
from moviexfone.paths import data_dir
from moviexfone.query import _clock_sentence
from moviexfone.shaping import ShowCard, render_speak, trim_cards
from moviexfone.sources.fetch import fetch_assemble_city
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import RequestBudget, refresh_assemble
from moviexfone.timeutil import NY, spoken_time
from moviexfone.voice.bridge import _resets_upstream_watch, _run_live

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def test_silence_prompt_does_not_end_the_call(settings: Settings, db: Database) -> None:
    class FakeUp:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[str] = asyncio.Queue()
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)
            if "session.update" in payload:
                await self.queue.put(json.dumps({"type": "session.updated"}))

        async def recv(self) -> str:
            return await self.queue.get()

        def __aiter__(self) -> FakeUp:
            return self

        async def __anext__(self) -> str:
            return await self.recv()

        async def close(self) -> None:
            return None

        async def __aenter__(self) -> FakeUp:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.client = types.SimpleNamespace(host="127.0.0.1")

        async def send_json(self, payload: dict[str, object]) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000) -> None:
            self.closed = code

        async def receive_json(self) -> dict[str, object]:
            return await self.incoming.get()

    upstream = FakeUp()
    module = types.ModuleType("websockets")
    module.connect = lambda *_a, **_k: upstream  # type: ignore[attr-defined]
    sys.modules["websockets"] = module
    client = FakeClient()
    live = Settings(
        xai_api_key="xai-test-key",
        live_voice=True,
        live_data=False,
        tool_secret=settings.tool_secret,
        db_path=settings.db_path,
        data_dir=settings.data_dir,
        bind=settings.bind,
        port=settings.port,
        cors_origins=settings.cors_origins,
        rate_limit_per_minute=60,
        assemble_ttl_seconds=900,
        xai_model="grok-voice-think-fast-2.0",
        max_session_seconds=30,
    )

    async def run() -> None:
        task = asyncio.create_task(
            _run_live(client, live, db, io_timeout=2, quiet_s=30, silence_after=0.15)  # type: ignore[arg-type]
        )
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("Anything else?" in payload for payload in upstream.sent):
                break
        assert any("Anything else?" in payload for payload in upstream.sent)
        assert not task.done()
        await client.incoming.put({"type": "close"})
        await asyncio.wait_for(task, timeout=2)
        assert not any("lost the line" in str(item.get("message", "")) for item in client.sent)

    asyncio.run(run())


def test_ping_does_not_reset_the_watchdog() -> None:
    assert _resets_upstream_watch("response.output_audio.delta")
    assert not _resets_upstream_watch("ping")
    assert not _resets_upstream_watch("pong")


def test_barclays_columbus_and_grand_central(db: Database) -> None:
    barclays = lookup_local("Atlantic Avenue Barclays Center station")
    assert barclays is not None
    assert barclays.get("ask") is not True
    assert barclays.get("borough") == "Brooklyn"
    theaters = load_theater_refs(db)
    places = load_places()
    circle = resolve_location("Columbus Circle", theaters, places)
    central = resolve_location("Grand Central", theaters, places)
    assert circle.radius_mi is not None and circle.radius_mi >= 1.0
    assert central.radius_mi is not None and central.radius_mi >= 1.0
    assert speakable_station("W 4 St-Wash Sq") == "West 4 Street Washington Square"


def test_film_ids_survive_a_live_reseed(tmp_path: object) -> None:
    from pathlib import Path

    db = Database(Path(str(tmp_path)) / "ids.db")
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    movie_id = _ensure_movie(db, "Street Fighter (2026)")
    db.execute("UPDATE movie SET tmdb_id = ? WHERE movie_id = ?", ("999001", movie_id))
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    again = _ensure_movie(db, "Street Fighter (2026)")
    row = db.query_one("SELECT tmdb_id FROM movie WHERE movie_id = ?", (again,))
    assert row is not None and str(row["tmdb_id"]) == "999001"
    db.close()


def test_refresh_does_not_drop_the_film_mid_fetch(db: Database, monkeypatch: object) -> None:
    _apply(
        db,
        [
            IncomingShow(
                theater_key="amc-empire-25",
                movie_title="Verity",
                starts_at=datetime(2026, 10, 14, 19, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://www.amctheatres.com/showtimes",
                source="assemble",
            )
        ],
        mode="insert",
        now=NOW,
    )

    def fake_city(
        _film: str, _slug: str, *, client: object = None, on_retry: object = None
    ) -> dict[str, object]:
        still = db.query_one(
            "SELECT COUNT(*) AS n FROM showtime WHERE source = 'assemble' AND movie_id = 'movie:verity'"
        )
        assert still is not None and int(still["n"]) >= 1
        return {
            "film_id": "1283515",
            "screenings": [
                {
                    "venue": {"name": "AMC Empire 25"},
                    "starts_at": "2026-10-14T21:00:00-04:00",
                    "availability": "on_sale",
                    "url": "https://www.amctheatres.com/showtimes",
                }
            ],
        }

    monkeypatch.setattr("moviexfone.sources.refresh.fetch_assemble_city", fake_city)  # type: ignore[attr-defined]
    refresh_assemble(
        db, now=NOW + timedelta(hours=2), budget=RequestBudget(), only_title="Verity"
    )
    times = [
        str(row["starts_at"])
        for row in db.query(
            "SELECT starts_at FROM showtime WHERE source = 'assemble' AND movie_id = 'movie:verity'"
        )
    ]
    assert any("21:00" in stamp for stamp in times)
    assert not any("19:00" in stamp for stamp in times)


def test_periods_and_year_suffix() -> None:
    assert _clock_sentence(
        "The closest listing I have is Verity at Regal on Friday at", spoken_time(time(10, 0))
    ) == ("The closest listing I have is Verity at Regal on Friday at ten a.m.")
    assert "a.m.." not in _clock_sentence("at", "ten a.m.")
    cards = [
        ShowCard(
            theater="AMC Empire",
            theater_id="amc-empire-25",
            movie="Street Fighter (2026)",
            movie_id="street-fighter",
            distance_mi=0.2,
            times_local=[time(10, 0), time(13, 0)],
            format_norm="unknown",
            ticket_url=None,
            source="assemble",
            borough="Manhattan",
        )
    ]
    _trimmed, _more, speak = trim_cards(
        cards,
        page=1,
        max_cards=1,
        max_times=1,
        when="Tomorrow",
        specific_movie=True,
    )
    assert "(2026)" not in speak
    assert "a.m," not in speak
    assert "a.m.." not in speak
    assert "Street Fighter" in render_speak(
        cards, when="Tomorrow", more=0, max_times=1, specific_movie=True
    )


def test_retries_call_the_budget() -> None:
    waits: list[str] = []

    class Response:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.headers = {"Retry-After": "0"}

        def json(self) -> dict[str, object]:
            return {}

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args: object, **_kwargs: object) -> Response:
            self.calls += 1
            if self.calls == 1:
                return Response(429)
            return Response(200)

    fetch_assemble_city(
        "1283515", "brooklyn-ny", client=Client(), on_retry=lambda: waits.append("retry")
    )  # type: ignore[arg-type]
    assert waits == ["retry"]


def test_error_is_spoken_in_live_mode() -> None:
    page = open("static/app.js", encoding="utf-8").read()
    assert 'if (mode === "live" && !force) return;' in page
    assert 'speak(msg.message || "", true);' in page
