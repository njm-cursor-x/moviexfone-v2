"""Regressions from the seventh live test."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from datetime import time as clock

from moviexfone.catalog import _apply, _ensure_movie
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.query import search_showtimes
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import FirstPartyRefresher
from moviexfone.timeutil import NY, spoken_time
from moviexfone.voice.loop import ToolLoop
from tests.test_round6 import _Client, _install, _live, _run_live, _Up

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def test_live_upstream_nudges_once(settings: Settings, db: Database) -> None:
    upstream = _Up()
    _install(upstream)
    client = _Client()

    async def run() -> None:
        task = asyncio.create_task(
            _run_live(
                client,  # type: ignore[arg-type]
                _live(settings),
                db,
                io_timeout=2,
                quiet_s=0.15,
                silence_after=5,
            )
        )
        for _ in range(30):
            await asyncio.sleep(0.02)
            if any(item.get("type") == "session.ready" for item in client.sent):
                break
        await upstream.queue.put(json.dumps({"type": "input_audio_buffer.committed"}))
        await upstream.queue.put(json.dumps({"type": "conversation.item.added"}))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("didn't catch that" in payload for payload in upstream.sent):
                break
        assert any("didn't catch that" in payload for payload in upstream.sent)
        assert not task.done()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("lost the line" in str(item.get("message", "")) for item in client.sent):
                break
        assert any("lost the line" in str(item.get("message", "")) for item in client.sent)
        await asyncio.wait_for(task, timeout=2)
        assert sum("didn't catch that" in payload for payload in upstream.sent) == 1

    asyncio.run(run())


def test_other_day_at_the_same_theater(db: Database) -> None:
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
    result = search_showtimes(
        db,
        location="Nitehawk Williamsburg",
        movie="Home Invasion 1",
        dates=["2026-10-18"],
        now=NOW,
        date_spoken="Sunday",
    )
    assert "Saturday" in result["speak"]
    assert spoken_time(clock(11, 15)) in result["speak"]
    assert "yet" not in result["speak"].lower()


def test_plain_title_offers_both_parts(db: Database) -> None:
    _ensure_movie(db, "Home Invasion 1")
    _ensure_movie(db, "Home Invasion 2")
    result = search_showtimes(
        db,
        location="Nitehawk Williamsburg",
        movie="Home Invasion",
        dates=["2026-10-17"],
        now=NOW,
        date_spoken="Saturday",
    )
    assert "Home Invasion 1" in result["speak"]
    assert "Home Invasion 2" in result["speak"]
    assert "don't have that title" not in result["speak"].lower()


def test_late_night_without_a_date_points_at_tomorrow(db: Database) -> None:
    _apply(
        db,
        [
            IncomingShow(
                theater_key="regal-union-square",
                movie_title="Verity",
                starts_at=datetime(2026, 10, 14, 11, 0, tzinfo=NY),
                format_norm="unknown",
                format_label=None,
                status="on_sale",
                ticket_url="https://www.regmovies.com/theatres/regal-union-square",
                source="assemble",
            )
        ],
        mode="insert",
        now=NOW,
    )
    result = search_showtimes(
        db,
        location="Union Square",
        dates=["2026-10-13"],
        now=datetime(2026, 10, 13, 23, 30, tzinfo=NY),
        date_spoken="today",
    )
    assert "Nothing else tonight" in result["speak"]
    assert "Tomorrow" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()


def test_spoken_goodbye_is_not_repeated() -> None:
    async def execute(_name: str, _args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "action": "end_call", "speak": "Enjoy the show."}

    loop = ToolLoop(execute)

    async def run() -> str:
        await loop.handle_server_event(
            {
                "type": "response.output_audio_transcript.delta",
                "delta": "You're welcome. Enjoy the show.",
            }
        )
        await loop.handle_server_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "thanks, goodbye",
            }
        )
        await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "bye",
                "name": "end_call",
                "arguments": "{}",
            }
        )
        done = await loop.handle_server_event({"type": "response.done"})
        return json.dumps(done)

    payload = asyncio.run(run())
    assert "call.end" in payload
    assert "force_message" not in payload


def test_first_party_refresh_runs_on_a_timer(db: Database, monkeypatch: object) -> None:
    calls: list[datetime] = []

    def fake(_db: Database, *, now: datetime) -> dict[str, object]:
        calls.append(now)
        return {}

    monkeypatch.setattr("moviexfone.catalog.refresh_live", fake)  # type: ignore[attr-defined]
    refresher = FirstPartyRefresher(db, interval_s=0.05)
    refresher.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not calls:
        time.sleep(0.02)
    refresher.stop()
    assert calls
