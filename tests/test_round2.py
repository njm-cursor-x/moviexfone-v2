"""Regressions from the second live test."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi.testclient import TestClient

from moviexfone.api.app import create_app
from moviexfone.api.security import VoiceTickets
from moviexfone.catalog import _apply, load_places, load_theater_refs
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.localgeo import lookup_local
from moviexfone.locations import resolve_location
from moviexfone.log import configure_logging
from moviexfone.nlu import parse_question
from moviexfone.query import search_showtimes
from moviexfone.sources.fetch import pick_geosearch
from moviexfone.sources.parse import IncomingShow, parse_alamo, parse_nitehawk_html
from moviexfone.timeutil import NY, resolve_when
from moviexfone.titles import resolve_title
from moviexfone.voice.loop import ToolLoop

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)
NIGHT = datetime(2026, 10, 14, 0, 30, tzinfo=NY)


def test_logs_redact_ticket_and_secret() -> None:
    from moviexfone.log import QueryRedactFilter

    configure_logging(["super-secret-value"])
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - "WebSocket /ws/voice?token=super-secret-value&ticket=abc" [accepted]',
        (),
        None,
    )
    assert QueryRedactFilter(["super-secret-value"]).filter(record)
    assert "super-secret-value" not in str(record.msg)
    assert "token=[redacted]" in str(record.msg)
    assert "ticket=[redacted]" in str(record.msg)


def test_ticket_is_single_use_and_secret_is_not_a_ticket() -> None:
    tickets = VoiceTickets()
    nonce = tickets.issue()
    assert tickets.consume(nonce) is True
    assert tickets.consume(nonce) is False
    assert tickets.consume("super-secret-value") is False


def test_page_does_not_put_the_secret_in_the_url() -> None:
    page = open("static/app.js", encoding="utf-8").read()
    assert 'URLSearchParams(location.search).get("token")' not in page
    assert "/v1/voice/ticket" in page
    assert "event.code === 1008" in page
    assert "event.code === 1013" in page


def test_assemble_rows_are_served_by_theater_name(db: Database) -> None:
    db.execute("UPDATE theater SET covered = 0")
    rows = [
        IncomingShow(
            theater_key="amc-lincoln-square-13",
            movie_title="Verity",
            starts_at=datetime(2026, 10, 14, 19, 0, tzinfo=NY),
            format_norm="unknown",
            format_label=None,
            status="on_sale",
            ticket_url="https://www.amctheatres.com/showtimes",
            source="assemble",
        )
    ]
    _apply(db, rows, mode="insert", now=NOW)
    result = search_showtimes(
        db,
        location="AMC Lincoln Square",
        movie="Verity",
        dates=["2026-10-14"],
        now=NOW,
        date_spoken="tomorrow",
    )
    assert result["results"]
    assert result["results"][0]["theater_id"] == "amc-lincoln-square-13"


def test_session_budget_covers_ten_minutes() -> None:
    from moviexfone.voice.bridge import _session_budget

    budget = _session_budget(900)
    assert budget > 125 * 64_000


def test_daypart_and_after_midnight_tomorrow() -> None:
    night = resolve_when("tomorrow night", NOW)
    assert night.dates == ["2026-10-14"]
    assert night.spoken == "tomorrow night"
    assert night.after is not None and night.after.hour == 17
    morning = resolve_when("tomorrow morning", NOW)
    assert morning.before is not None and morning.before.hour == 12
    late = resolve_when("tomorrow", NIGHT)
    assert late.dates == ["2026-10-14"]
    iso = resolve_when("2026-10-13", NOW)
    assert iso.spoken == "today"
    nxt = resolve_when("next Tuesday", NOW)
    assert nxt.spoken.startswith("next ")


def test_zip_and_station_stay_local() -> None:
    williamsburg = lookup_local("11211")
    assert williamsburg is not None
    assert williamsburg["borough"] == "Brooklyn"
    heights = lookup_local("11201")
    assert heights is not None
    assert "Queens" not in str(heights["label"])
    station = lookup_local("Bedford Avenue station")
    assert station is not None
    assert station["borough"] == "Brooklyn"
    missed = pick_geosearch(
        {
            "features": [
                {
                    "properties": {
                        "name": "11211 ROOSEVELT AVENUE",
                        "housenumber": "11211",
                        "layer": "address",
                    },
                    "geometry": {"coordinates": [-73.9, 40.7]},
                }
            ]
        },
        "11211",
    )
    assert missed is None


def test_upper_west_side_wins(db: Database) -> None:
    resolution = resolve_location("Upper West Side", load_theater_refs(db), load_places())
    assert resolution.kind == "place"
    assert resolution.label == "Upper West Side"


def test_visible_nitehawk_times() -> None:
    html = """
    <div class="show-title">Other Mommy</div>
    <div class="showtimes-container">
      <ul>
        <li data-date="1791874800"><span class="showtime past">2:45 pm</span></li>
        <li data-date="1791874800">
          <a class="showtime" href="https://nitehawkcinema.com/prospectpark/purchase/24497158/">7:35 pm</a>
        </li>
      </ul>
    </div>
    """
    rows = parse_nitehawk_html(html, default_theater="nitehawk-cinema-prospect-park")
    assert len(rows) == 1
    assert rows[0].movie_title == "Other Mommy"
    assert rows[0].starts_at.hour == 19


def test_alamo_rating_and_synopsis() -> None:
    rows = parse_alamo(
        {
            "data": {
                "presentations": [
                    {
                        "slug": "verity",
                        "show": {
                            "title": "Verity",
                            "certification": "R",
                            "headline": "A writer takes a dangerous job.",
                        },
                    }
                ],
                "market": [{"cinemas": [{"id": "1", "name": "Lower Manhattan"}]}],
                "sessions": [
                    {
                        "cinemaId": "1",
                        "presentationSlug": "verity",
                        "showTimeClt": "2026-10-13T19:15:00-04:00",
                        "status": "onsale",
                        "formatSlug": "digital",
                        "sessionId": "s1",
                    }
                ],
            }
        }
    )
    assert rows[0].rating == "R"
    assert rows[0].synopsis and "dangerous" in rows[0].synopsis


def test_cancel_only_when_a_response_is_active() -> None:
    loop = ToolLoop(lambda _name, _args: {})

    async def run() -> None:
        idle = await loop.handle_server_event({"type": "input_audio_buffer.speech_started"})
        assert idle == []
        await loop.handle_server_event({"type": "response.created", "response": {"id": "r1"}})
        cancel = await loop.handle_server_event({"type": "input_audio_buffer.speech_started"})
        assert cancel == [{"type": "response.cancel"}]

    import asyncio

    asyncio.run(run())


def test_direct_speak_uses_force_message() -> None:
    async def execute(_name: str, _args: dict[str, object]) -> dict[str, object]:
        return {"speak": "Tonight at seven."}

    loop = ToolLoop(execute)

    async def run() -> list[dict[str, object]]:
        await loop.handle_server_event(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c1",
                "name": "search_showtimes",
                "arguments": "{}",
            }
        )
        return await loop.handle_server_event({"type": "response.done"})

    import asyncio

    messages = asyncio.run(run())
    kinds = [item.get("type") for item in messages]
    assert "conversation.item.create" in kinds
    assert "response.create" not in kinds


def test_word_inside_one_title_is_a_suggestion() -> None:
    catalog = [
        ("social-reckoning", "The Social Reckoning", ["the social reckoning"]),
        ("other-mommy", "Other Mommy", ["other mommy"]),
    ]
    hit, suggestions = resolve_title("social", catalog)
    assert hit is None
    assert suggestions and suggestions[0].title == "The Social Reckoning"


def test_night_question_sets_after_five() -> None:
    parsed = parse_question(
        "what's playing tomorrow night near Union Square",
        now=NOW,
        titles=[("Verity", ["verity"])],
    )
    assert parsed.after_time == "17:00"
    assert parsed.date_spoken == "tomorrow night"


def test_ticket_route_and_log(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        ticket = client.post("/v1/voice/ticket", headers={"origin": "http://127.0.0.1:8000"})
        assert ticket.status_code == 200
        nonce = ticket.json()["ticket"]
        assert nonce != "super-secret-value"
        with client.websocket_connect(
            f"/ws/voice?ticket={nonce}", headers={"origin": "http://127.0.0.1:8000"}
        ) as socket:
            ready = socket.receive_json()
        assert ready["type"] == "session.ready"
