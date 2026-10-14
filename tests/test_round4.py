"""Regressions from the fourth live test."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, time

from moviexfone.api.security import SessionCaps
from moviexfone.catalog import _apply, seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.localgeo import lookup_local
from moviexfone.paths import data_dir
from moviexfone.sanitize import sanitize_display
from moviexfone.shaping import ShowCard, trim_cards
from moviexfone.sources.fetch import normalize_film_query
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import GLOBAL_BUDGET, RequestBudget
from moviexfone.timeutil import NY
from moviexfone.voice.bridge import (
    _UPSTREAM_SLOTS,
    _run_live,
    abort_socket,
    await_io,
    upstream_is_quiet,
)

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def test_upstream_silence_and_stalled_send() -> None:
    assert upstream_is_quiet(0, 21, expecting=True, quiet_s=20)
    assert not upstream_is_quiet(0, 21, expecting=False, quiet_s=20)

    async def hang(_payload: str) -> None:
        await asyncio.Event().wait()

    async def run() -> str:
        try:
            await await_io(hang("frame"), 0.05)
        except TimeoutError:
            return "released"
        return "held"

    assert asyncio.run(run()) == "released"

    class Dead:
        def abort(self) -> None:
            self.aborted = True

        transport = None

    sock = Dead()
    sock.transport = sock
    abort_socket(sock)
    assert sock.aborted is True


def test_stalled_upstream_frees_the_line(settings: Settings, db: Database) -> None:
    upstream_sent: list[str] = []

    class FakeUp:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, payload: str) -> None:
            upstream_sent.append(payload[:40])
            if "session.update" in payload:
                await self.queue.put(json.dumps({"type": "session.updated"}))
            if "input_audio_buffer" in payload:
                await asyncio.Event().wait()

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

    module = types.ModuleType("websockets")
    module.connect = lambda *_a, **_k: FakeUp()  # type: ignore[attr-defined]
    sys.modules["websockets"] = module
    client = FakeClient()
    client.incoming.put_nowait({"type": "audio", "data": "AAAA"})
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
        await asyncio.wait_for(
            _run_live(client, live, db, io_timeout=0.1, quiet_s=30),  # type: ignore[arg-type]
            timeout=3,
        )

    asyncio.run(run())
    assert any("lost the line" in str(item.get("message", "")) for item in client.sent)
    assert _UPSTREAM_SLOTS.acquire(blocking=False)
    _UPSTREAM_SLOTS.release()


def test_assemble_budget_is_shared() -> None:
    from moviexfone.sources.refresh import refresh_assemble

    assert refresh_assemble.__defaults__ is None or GLOBAL_BUDGET.limit <= 50
    assert isinstance(GLOBAL_BUDGET, RequestBudget)
    assert GLOBAL_BUDGET.limit <= 50


def test_nitehawk_times_survive_assemble(db: Database) -> None:
    nitehawk = IncomingShow(
        theater_key="nitehawk-cinema-prospect-park",
        movie_title="The Social Reckoning",
        starts_at=datetime(2026, 10, 18, 18, 45, tzinfo=NY),
        format_norm="unknown",
        format_label=None,
        status="on_sale",
        ticket_url="https://nitehawkcinema.com/prospectpark/purchase/1/",
        source="nitehawk_jsonld",
    )
    assemble = IncomingShow(
        theater_key="nitehawk-cinema-prospect-park",
        movie_title="The Social Reckoning",
        starts_at=datetime(2026, 10, 18, 17, 25, tzinfo=NY),
        format_norm="unknown",
        format_label=None,
        status="on_sale",
        ticket_url="https://www.amctheatres.com/showtimes",
        source="assemble",
    )
    _apply(db, [nitehawk], mode="insert", now=NOW)
    _apply(db, [assemble], mode="insert", now=NOW)
    rows = db.query(
        """SELECT starts_at, source FROM showtime
           WHERE theater_id = 'nitehawk-cinema-prospect-park'
             AND business_date = '2026-10-18'"""
    )
    assert any(
        row["source"] == "nitehawk_jsonld" and "18:45" in str(row["starts_at"]) for row in rows
    )
    assert not any("17:25" in str(row["starts_at"]) for row in rows)


def test_want_more_is_not_spliced() -> None:
    moment = time(21, 0)
    cards = [
        ShowCard(
            theater="AMC Empire",
            theater_id="amc-empire-25",
            movie="Verity",
            movie_id="verity",
            distance_mi=0.4,
            times_local=[moment, time(22, 0), time(23, 30)],
            format_norm="unknown",
            ticket_url=None,
            source="assemble",
            borough="Manhattan",
        ),
        ShowCard(
            theater="AMC Empire",
            theater_id="amc-empire-25",
            movie="Digger",
            movie_id="digger",
            distance_mi=0.4,
            times_local=[time(19, 0)],
            format_norm="unknown",
            ticket_url=None,
            source="assemble",
            borough="Manhattan",
        ),
    ]
    _trimmed, more, speak = trim_cards(
        cards,
        page=1,
        max_cards=2,
        max_times=1,
        when="Tomorrow",
        specific_movie=False,
        name_theater=True,
    )
    assert "Want more?, and" not in speak
    if more or "more" in speak:
        assert "and" in speak
        assert not speak.endswith("Want more?, and 2 more.")


def test_rejected_hold_does_not_free_the_live_caller() -> None:
    caps = SessionCaps(per_ip=1, daily=5)
    assert caps.hold("10.0.0.8") is None
    assert caps.hold("10.0.0.8") is not None
    # A rejected attempt must not be released; that used to drop the live count.
    assert caps.hold("10.0.0.8") is not None
    caps.release("10.0.0.8")
    assert caps.hold("10.0.0.8") is None


def test_daily_cap_persists_and_ignores_failed_attempts(tmp_path: object) -> None:
    from pathlib import Path

    db = Database(Path(str(tmp_path)) / "caps.db")
    caps = SessionCaps(db, per_ip=2, daily=1)
    assert caps.hold("10.1.1.1") is None
    caps.release("10.1.1.1")
    assert caps.hold("10.1.1.1") is None
    caps.commit("10.1.1.1")
    again = SessionCaps(db, per_ip=2, daily=1)
    assert "today" in str(again.hold("10.1.1.1"))
    db.close()


def test_live_reseed_keeps_showtimes(tmp_path: object) -> None:
    from pathlib import Path

    db = Database(Path(str(tmp_path)) / "keep.db")
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    db.execute(
        "UPDATE movie SET tmdb_id = ? WHERE title = ?",
        ("1400837", "Other Mommy"),
    )
    _apply(
        db,
        [
            IncomingShow(
                theater_key="amc-empire-25",
                movie_title="Other Mommy",
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
    seed(db, data_dir(), now=NOW, include_showtimes=False)
    kept = db.query_one("SELECT COUNT(*) AS n FROM showtime WHERE source = 'assemble'")
    film = db.query_one("SELECT tmdb_id FROM movie WHERE title = 'Other Mommy'")
    assert kept is not None and int(kept["n"]) == 1
    assert film is not None and str(film["tmdb_id"]) == "1400837"
    db.close()


def test_year_suffix_and_html_entities() -> None:
    title, year = normalize_film_query("Street Fighter (2026)")
    assert title == "Street Fighter"
    assert year == "2026"
    plain, _year = normalize_film_query("Trick &#8216;r Treat")
    assert "&#" not in plain
    assert "Trick" in plain
    spoken = sanitize_display("J.D.&#8217;s Revenge")
    assert "&#" not in spoken
    assert "Revenge" in spoken


def test_named_stations_resolve() -> None:
    atlantic = lookup_local("Atlantic Av–Barclays")
    assert atlantic is not None and atlantic.get("borough") == "Brooklyn"
    west = lookup_local("W 4th St")
    assert west is not None and "Wash" in str(west["label"])
    circle = lookup_local("Columbus Circle")
    assert circle is not None and "Columbus" in str(circle["label"])
    central = lookup_local("Grand Central")
    assert central is not None and central.get("borough") == "Manhattan"
    bedford = lookup_local("Bedford Av station")
    assert bedford is not None and "Avenue" in str(bedford["label"])
