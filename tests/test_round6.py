"""Regressions from the sixth live test."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta

from moviexfone.catalog import _apply, _ensure_movie
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.localgeo import speakable_station
from moviexfone.query import search_showtimes
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import RequestBudget, refresh_assemble
from moviexfone.timeutil import NY
from moviexfone.titles import score_title
from moviexfone.voice.bridge import _run_live
from moviexfone.voice.loop import ToolLoop

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


class _Up:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if "session.update" in payload:
            await self.queue.put(json.dumps({"type": "session.updated"}))

    async def recv(self) -> str:
        return await self.queue.get()

    def __aiter__(self) -> _Up:
        return self

    async def __anext__(self) -> str:
        return await self.recv()

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _Up:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Client:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.closed: int | None = None

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def receive_json(self) -> dict[str, object]:
        return await self.incoming.get()


def _install(upstream: _Up) -> None:
    module = types.ModuleType("websockets")
    module.connect = lambda *_a, **_k: upstream  # type: ignore[attr-defined]
    sys.modules["websockets"] = module


def _live(settings: Settings) -> Settings:
    return Settings(
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


def test_pauses_do_not_hang_up_and_a_long_silence_asks_once(
    settings: Settings, db: Database
) -> None:
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
                silence_after=0.55,
            )
        )
        for _ in range(30):
            await asyncio.sleep(0.02)
            if any(item.get("type") == "session.ready" for item in client.sent):
                break
        await upstream.queue.put(json.dumps({"type": "response.done"}))
        # Mic frames keep arriving through a pause. They are not a turn.
        for _ in range(8):
            await client.incoming.put({"type": "audio", "data": "frame"})
            await asyncio.sleep(0.04)
        assert not task.done()
        assert not any("didn't catch that" in payload for payload in upstream.sent)
        assert not any("lost the line" in str(item.get("message", "")) for item in client.sent)

        def prompts() -> int:
            return sum('"text": "Anything else?"' in payload for payload in upstream.sent)

        for _ in range(40):
            await asyncio.sleep(0.05)
            if prompts():
                break
        assert prompts() == 1
        await asyncio.sleep(0.35)
        assert prompts() == 1
        assert not task.done()
        assert not any("lost the line" in str(item.get("message", "")) for item in client.sent)
        await client.incoming.put({"type": "close"})
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())


def test_committed_noise_nudges_once_then_ends(settings: Settings, db: Database) -> None:
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
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("didn't catch that" in payload for payload in upstream.sent):
                break
        assert any("didn't catch that" in payload for payload in upstream.sent)
        assert not task.done()
        assert not any("lost the line" in str(item.get("message", "")) for item in client.sent)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("lost the line" in str(item.get("message", "")) for item in client.sent):
                break
        assert any("lost the line" in str(item.get("message", "")) for item in client.sent)
        await asyncio.wait_for(task, timeout=2)
        assert sum("didn't catch that" in payload for payload in upstream.sent) == 1

    asyncio.run(run())


def test_sequel_numbers_stay_distinct() -> None:
    assert score_title("Home Invasion 2", "Home Invasion 1") == 0
    assert score_title("Home Invasion II", "Home Invasion 2") == 1
    assert score_title("Home Invasion", "Home Invasion 2") == 0


def test_evening_after_the_last_show_points_at_tomorrow(db: Database) -> None:
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
        after_time="17:00",
        now=datetime(2026, 10, 13, 23, 30, tzinfo=NY),
        date_spoken="today",
    )
    assert "Nothing else tonight" in result["speak"]
    assert "Tomorrow" in result["speak"]
    assert "don't have live times" not in result["speak"].lower()


def test_unfetched_title_does_not_speak_the_year(db: Database) -> None:
    _ensure_movie(db, "Street Fighter (2026)")
    result = search_showtimes(
        db,
        location="AMC Empire",
        movie="Street Fighter (2026)",
        dates=["2026-10-14"],
        now=NOW,
        date_spoken="tomorrow",
    )
    assert "(2026)" not in result["speak"]
    assert "Street Fighter" in result["speak"]


def test_negative_film_lookups_last_six_hours(db: Database, monkeypatch: object) -> None:
    calls = {"n": 0}

    def missing(_title: str, *, client: object = None) -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr("moviexfone.sources.refresh.lookup_film_id", missing)  # type: ignore[attr-defined]
    refresh_assemble(db, now=NOW, budget=RequestBudget(), only_title="Digger")
    refresh_assemble(
        db, now=NOW + timedelta(minutes=20), budget=RequestBudget(), only_title="Digger"
    )
    assert calls["n"] == 1
    refresh_assemble(db, now=NOW + timedelta(hours=7), budget=RequestBudget(), only_title="Digger")
    assert calls["n"] == 2


def test_street_numbers_are_spoken() -> None:
    assert speakable_station("Grand Central-42 St") == "Grand Central Forty-second Street"
    assert speakable_station("W 4 St") == "West Fourth Street"


def test_goodbye_is_forced_before_the_line_closes() -> None:
    async def execute(_name: str, _args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "action": "end_call", "speak": "Enjoy the show."}

    loop = ToolLoop(execute)

    async def run() -> tuple[str, str]:
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
        spoken = await loop.handle_server_event({"type": "response.done"})
        closed = await loop.handle_server_event({"type": "response.done"})
        return json.dumps(spoken), str(closed[-1].get("type"))

    spoken, closed = asyncio.run(run())
    assert "Enjoy the show." in spoken
    assert "call.end" not in spoken
    assert closed == "call.end"
    page = open("static/app.js", encoding="utf-8").read()
    assert 'msg.type === "goodbye"' in page
    readme = open("README.md", encoding="utf-8").read()
    assert "per process" in readme
