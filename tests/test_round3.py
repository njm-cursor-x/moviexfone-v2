"""Regressions from the third live test."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

from moviexfone.api.security import SessionCaps, enforce_deployment, ticket_allowed
from moviexfone.catalog import load_places, load_theater_refs
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.localgeo import lookup_local
from moviexfone.locations import resolve_location
from moviexfone.query import search_showtimes
from moviexfone.shaping import ShowCard, render_speak
from moviexfone.sources.fetch import lookup_film_id, nitehawk_day_url
from moviexfone.sources.refresh import AssembleRefresher
from moviexfone.timeutil import NY
from moviexfone.voice.loop import ToolLoop
from moviexfone.voice.schemas import SEARCH_SHOWTIMES

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)


def test_film_key_and_text_json_resolve_without_rest_search() -> None:
    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, **_kwargs: object) -> object:
            self.urls.append(url)
            raise AssertionError(url)

        def post(self, url: str, **_kwargs: object) -> object:
            self.urls.append(url)
            body = {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"matches": [{"film": "1400837", "title": "Other Mommy"}]}
                            ),
                        }
                    ],
                    "structuredContent": {"matches": [{"film": "1400837", "title": "Other Mommy"}]},
                }
            }

            class Response:
                status_code = 200

                def json(self) -> dict[str, object]:
                    return body

            return Response()

    client = Client()
    assert lookup_film_id("Other Mommy", client=client) == "1400837"  # type: ignore[arg-type]
    assert client.urls == ["https://api.assemble.film/mcp"]
    assert "/films/search" not in "".join(client.urls)


def test_warmup_queues_every_title(db: Database) -> None:
    refresher = AssembleRefresher(db)
    refresher.start()
    titles = {refresher._queue.get(timeout=2) for _ in range(refresher._queue.qsize())}
    refresher.stop()
    refresher._thread.join(timeout=2)
    assert "Other Mommy" in titles
    assert "Verity" in titles
    assert len(titles) >= 5


def test_unfetched_title_is_not_called_absent(db: Database) -> None:
    db.execute("DELETE FROM showtime WHERE movie_id = 'movie:other-mommy'")
    result = search_showtimes(
        db,
        location="Regal Union Square",
        movie="Other Mommy",
        dates=["2026-10-14"],
        now=NOW,
        date_spoken="tomorrow",
    )
    assert "yet" in result["speak"].lower()
    assert "no screenings listed" not in result["speak"].lower()


def test_silence_does_not_hang_up() -> None:
    async def execute(_name: str, _args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "action": "end_call"}

    loop = ToolLoop(execute)

    async def run() -> list[str]:
        await loop.handle_server_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "",
            }
        )
        await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c",
                "name": "end_call",
                "arguments": "{}",
            }
        )
        first = await loop.handle_server_event({"type": "response.done"})
        second = await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "d",
                "name": "end_call",
                "arguments": "{}",
            }
        )
        # The second tool call is pending until response.done.
        second_done = await loop.handle_server_event({"type": "response.done"})
        kinds = [item.get("type") for item in [*first, *second, *second_done]]
        await loop.handle_server_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "thanks, goodbye",
            }
        )
        await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "e",
                "name": "end_call",
                "arguments": "{}",
            }
        )
        goodbye = await loop.handle_server_event({"type": "response.done"})
        spoken = json.dumps(goodbye)
        closed = await loop.handle_server_event({"type": "response.done"})
        return [*kinds, spoken, *[str(item.get("type")) for item in closed]]

    kinds = asyncio.run(run())
    assert "call.end" not in kinds[:-1]
    assert "Enjoy the show." in kinds[-2]
    assert kinds[-1] == "call.end"


def test_spoofed_origin_cannot_mint_a_ticket() -> None:
    assert (
        ticket_allowed(
            origin="https://moviexfone.example",
            host="172.30.0.2",
            header_secret=None,
            allowed_origins=("https://moviexfone.example",),
            secret="a-long-random-string",
        )
        is False
    )
    caps = SessionCaps(per_ip=1, daily=2)
    assert caps.hold("172.30.0.2") is None
    assert caps.hold("172.30.0.2") is not None
    caps.release("172.30.0.2")
    caps.commit("172.30.0.2")
    assert caps.hold("172.30.0.2") is None
    caps.release("172.30.0.2")
    caps.commit("172.30.0.2")
    assert "today" in str(caps.hold("172.30.0.2"))


def test_public_bind_requires_basic_auth(tmp_path: object) -> None:
    from pathlib import Path

    root = Path("data")
    public = Settings(
        xai_api_key=None,
        live_voice=False,
        live_data=False,
        tool_secret="a-long-random-string",
        db_path=tmp_path / "a.db",  # type: ignore[operator]
        data_dir=root,
        bind="0.0.0.0",
        port=8000,
        cors_origins=(),
        rate_limit_per_minute=60,
        assemble_ttl_seconds=900,
        xai_model="grok-voice-think-fast-2.0",
        max_session_seconds=900,
    )
    try:
        enforce_deployment(public)
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_station_abbreviations_and_lines() -> None:
    bedford = lookup_local("Bedford Ave L stop")
    assert bedford is not None
    assert "Bedford" in str(bedford["label"])
    assert bedford["borough"] == "Brooklyn"
    eighth = lookup_local("8th Avenue L stop")
    assert eighth is not None
    assert eighth["borough"] == "Manhattan"
    circle = lookup_local("59th Street Columbus Circle station")
    assert circle is not None
    assert "Columbus" in str(circle["label"])
    missed = lookup_local("Not A Real Station stop")
    assert missed is not None and missed.get("ask") is True


def test_unknown_station_asks_instead_of_a_borough(db: Database) -> None:
    resolution = resolve_location(
        "Not A Real Station stop",
        load_theater_refs(db),
        load_places(),
        geocode=lambda _text: {"label": "Brooklyn", "lat": 40.65, "lng": -73.95},
    )
    assert resolution.kind == "not_found"


def test_nitehawk_future_days_use_a_path() -> None:
    url = nitehawk_day_url(
        "https://nitehawkcinema.com/prospectpark", date(2026, 10, 14), first=False
    )
    assert url == "https://nitehawkcinema.com/prospectpark/2026-10-14/1/"
    today = nitehawk_day_url(
        "https://nitehawkcinema.com/prospectpark", date(2026, 10, 14), first=True
    )
    assert today == "https://nitehawkcinema.com/prospectpark/2026-10-14/1/"


def test_place_answer_names_the_theater_once() -> None:
    card = ShowCard(
        theater="Nitehawk Williamsburg",
        theater_id="nitehawk-cinema-williamsburg",
        movie="Digger",
        movie_id="digger",
        distance_mi=0.2,
        times_local=[],
        format_norm="unknown",
        ticket_url=None,
        source="nitehawk_jsonld",
        borough="Brooklyn",
    )
    # times_local empty would make clocks empty; use a real time via a second card pair in query.
    spoken = render_speak(
        [
            ShowCard(
                theater="Nitehawk Williamsburg",
                theater_id="nitehawk-cinema-williamsburg",
                movie="Digger",
                movie_id="digger",
                distance_mi=0.2,
                times_local=[datetime(2026, 10, 13, 21, 0, tzinfo=NY).timetz()],
                format_norm="unknown",
                ticket_url=None,
                source="nitehawk_jsonld",
                borough="Brooklyn",
            ),
            ShowCard(
                theater="Nitehawk Williamsburg",
                theater_id="nitehawk-cinema-williamsburg",
                movie="Verity",
                movie_id="verity",
                distance_mi=0.2,
                times_local=[datetime(2026, 10, 13, 21, 15, tzinfo=NY).timetz()],
                format_norm="unknown",
                ticket_url=None,
                source="nitehawk_jsonld",
                borough="Brooklyn",
            ),
        ],
        when="Tonight",
        more=0,
        max_times=2,
        specific_movie=False,
        name_theater=False,
    )
    assert spoken.lower().count("nitehawk") == 1
    del card


def test_before_time_is_a_voice_argument() -> None:
    props = SEARCH_SHOWTIMES["parameters"]["properties"]
    assert "before_time" in props


def test_page_stops_when_the_ticket_is_refused() -> None:
    page = open("static/app.js", encoding="utf-8").read()
    assert "response.status === 401" in page
    assert "if (ticketFailed) return;" in page
    assert "socket._statusShown" in page
