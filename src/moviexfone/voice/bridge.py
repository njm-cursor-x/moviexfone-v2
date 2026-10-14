"""WebSocket bridge between the browser and xAI realtime, plus a mock mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from moviexfone.api.security import voice_connection_allowed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.service import ask
from moviexfone.sources.fetch import geosearch
from moviexfone.timeutil import now_ny, resolve_when
from moviexfone.voice.loop import ToolLoop
from moviexfone.voice.prompt import GREETING, system_prompt
from moviexfone.voice.schemas import tool_schemas

log = logging.getLogger(__name__)

_UPSTREAM_SLOTS = threading.BoundedSemaphore(1)
_MAX_UPSTREAM_BYTES = 1_048_576
_MAX_FRAME_CHARS = 65_536
_MAX_SESSION_CHARS = 8_000_000


async def handle_voice_socket(
    websocket: WebSocket,
    settings: Settings,
    db: Database,
    refresher: Any | None = None,
    tickets: Any | None = None,
) -> None:
    await websocket.accept()
    host = websocket.client.host if websocket.client else None
    ticket = websocket.query_params.get("ticket") or websocket.query_params.get("token")
    allowed, reason = voice_connection_allowed(
        origin=websocket.headers.get("origin"),
        host=host,
        token=ticket,
        allowed_origins=settings.cors_origins,
        secret=settings.tool_secret,
        live=settings.live_voice,
        tickets=tickets,
    )
    if not allowed:
        await websocket.send_json({"type": "error", "message": reason or "not authorized"})
        await websocket.close(code=1008)
        return
    if settings.voice_mode == "live":
        try:
            await _run_live(websocket, settings, db, refresher)
            return
        except WebSocketDisconnect:
            return
        except Exception:
            log.warning("live voice failed; using mock mode")
            try:
                await websocket.send_json(
                    {
                        "type": "notice",
                        "message": "Live voice is unavailable. This browser will read the answer itself.",
                    }
                )
            except Exception:
                return
    try:
        await _run_mock(websocket, db)
    except WebSocketDisconnect:
        return


def _geocode(text: str) -> dict[str, Any] | None:
    try:
        return geosearch(text)
    except Exception as exc:
        log.info("geosearch failed: %s", type(exc).__name__)
        return None


async def _run_mock(websocket: WebSocket, db: Database) -> None:
    from starlette.websockets import WebSocketDisconnect

    await websocket.send_json({"type": "session.ready", "mode": "mock", "greeting": GREETING})
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "text":
                text = str(message.get("text") or "")[:400]
                payload = ask(db, text, now=now_ny())
                await websocket.send_json({"type": "transcript", "role": "user", "text": text})
                await websocket.send_json(
                    {
                        "type": "transcript",
                        "role": "assistant",
                        "text": str(payload.get("speak") or ""),
                    }
                )
                await websocket.send_json({"type": "showtimes", "payload": payload})
            elif kind == "close":
                await websocket.close()
                return
    except WebSocketDisconnect:
        return


def _session_budget(seconds: int) -> int:
    return int(max(60, seconds) * 64_000 * 1.2)


async def _run_live(
    websocket: WebSocket,
    settings: Settings,
    db: Database,
    refresher: Any | None = None,
) -> None:
    import websockets

    if not settings.xai_api_key:
        raise RuntimeError("missing key")
    if not _UPSTREAM_SLOTS.acquire(blocking=False):
        await websocket.send_json({"type": "error", "message": "The line is busy. Try again soon."})
        await websocket.close(code=1013)
        return
    url = f"wss://api.x.ai/v1/realtime?model={settings.xai_model}"
    headers = {"Authorization": f"Bearer {settings.xai_api_key}"}
    held = True

    def release_slot() -> None:
        nonlocal held
        if held:
            _UPSTREAM_SLOTS.release()
            held = False

    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            max_size=_MAX_UPSTREAM_BYTES,
            open_timeout=20,
            close_timeout=1,
        ) as upstream:
            await upstream.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": _session_body(db),
                    }
                )
            )
            try:
                async with asyncio.timeout(10):
                    while True:
                        raw = await upstream.recv()
                        if isinstance(raw, bytes):
                            continue
                        event = json.loads(raw)
                        if event.get("type") == "session.updated":
                            break
                        if event.get("type") == "error":
                            raise RuntimeError("session update was rejected")
            except TimeoutError:
                await websocket.send_json(
                    {"type": "error", "message": "The voice service did not answer."}
                )
                await websocket.close(code=1011)
                return
            await upstream.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "force_message",
                            "role": "assistant",
                            "interruptible": True,
                            "content": [{"type": "output_text", "text": GREETING}],
                        },
                    }
                )
            )
            await websocket.send_json(
                {"type": "session.ready", "mode": "live", "greeting": GREETING}
            )
            last_sound = time.monotonic()
            silence_prompted = False

            def heard() -> None:
                nonlocal last_sound
                last_sound = time.monotonic()

            loop = ToolLoop(
                lambda name, args: _execute(
                    db, name, args, live_data=settings.live_data, refresher=refresher
                ),
                send=_send_upstream(upstream),
            )

            async def client_to_upstream() -> None:
                received = 0
                while True:
                    message = await websocket.receive_json()
                    kind = message.get("type")
                    if kind == "audio":
                        data = message.get("data") or ""
                        if not isinstance(data, str) or len(data) > _MAX_FRAME_CHARS:
                            await websocket.close(code=1009)
                            return
                        received += len(data)
                        heard()
                        if received > _session_budget(settings.max_session_seconds):
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": "This call has reached its length.",
                                }
                            )
                            await websocket.close(code=1000)
                            return
                        await upstream.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": message.get("data") or "",
                                }
                            )
                        )
                    elif kind == "text":
                        await upstream.send(
                            json.dumps(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": str(message.get("text") or "")[:400],
                                            }
                                        ],
                                    },
                                }
                            )
                        )
                        await upstream.send(json.dumps({"type": "response.create"}))
                    elif kind == "close":
                        return

            async def upstream_to_client() -> None:
                async for raw in upstream:
                    if isinstance(raw, bytes):
                        continue
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "response.output_audio.delta":
                        heard()
                    for outbound_event in _client_events(event):
                        await websocket.send_json(outbound_event)
                    if kind == "conversation.item.input_audio_transcription.completed":
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "role": "user",
                                "text": event.get("transcript") or "",
                            }
                        )
                    elif kind == "response.done":
                        await websocket.send_json({"type": "transcript.done", "role": "assistant"})
                    elif kind == "error":
                        detail = json.dumps(event)
                        if "no active response" in detail.lower():
                            log.info("ignored cancel with no active response")
                        else:
                            await websocket.send_json(
                                {"type": "error", "message": "The voice service returned an error."}
                            )
                            log.warning("realtime error type=%s", kind)
                    outbound = await loop.handle_server_event(event)
                    for item in outbound:
                        if item.get("type") == "call.end":
                            await websocket.send_json({"type": "call.ended"})
                            await websocket.close(code=1000)
                            return
                        await upstream.send(json.dumps(item))
                        output = item.get("item")
                        if (
                            isinstance(output, dict)
                            and output.get("type") == "function_call_output"
                        ):
                            try:
                                payload = json.loads(str(output.get("output") or "{}"))
                            except json.JSONDecodeError:
                                payload = {}
                            if isinstance(payload, dict) and payload.get("speak"):
                                await websocket.send_json({"type": "showtimes", "payload": payload})

            async def silence_once() -> None:
                nonlocal silence_prompted
                while not silence_prompted:
                    await asyncio.sleep(5)
                    if time.monotonic() - last_sound < 40:
                        continue
                    silence_prompted = True
                    await upstream.send(
                        json.dumps(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "force_message",
                                    "role": "assistant",
                                    "interruptible": True,
                                    "content": [{"type": "output_text", "text": "Anything else?"}],
                                },
                            }
                        )
                    )
                    return

            client_task = asyncio.create_task(client_to_upstream())
            upstream_task = asyncio.create_task(upstream_to_client())
            silence_task = asyncio.create_task(silence_once())
            limit = asyncio.create_task(asyncio.sleep(settings.max_session_seconds))
            try:
                done, _pending = await asyncio.wait(
                    {client_task, upstream_task, limit},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if limit in done and client_task not in done:
                    await websocket.send_json(
                        {"type": "error", "message": "This call has reached its time limit."}
                    )
                if limit in done:
                    try:
                        await websocket.close(code=1000)
                    except Exception:
                        log.info("time cap close")
            finally:
                release_slot()
                for task in (client_task, upstream_task, silence_task, limit):
                    task.cancel()
                await asyncio.gather(
                    client_task, upstream_task, silence_task, limit, return_exceptions=True
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(upstream.close(), timeout=1)
    finally:
        release_slot()


def _session_body(db: Database) -> dict[str, Any]:
    from moviexfone.sanitize import sanitize_display

    titles = [
        sanitize_display(str(row["title"]), limit=50)
        for row in db.query("SELECT title FROM movie ORDER BY title")
    ]
    theaters = [
        sanitize_display(str(row["spoken_name"]), limit=50)
        for row in db.query(
            "SELECT spoken_name FROM theater WHERE status = 'open' ORDER BY spoken_name LIMIT 40"
        )
    ]
    keyterms = [term for term in [*titles, *theaters] if term][:100]
    return {
        "voice": "rex",
        "instructions": system_prompt(now_ny()),
        "turn_detection": {"type": "server_vad"},
        "reasoning": {"effort": "none"},
        "tools": tool_schemas(),
        "replace": {"Nitehawk": "Night hawk", "IMAX": "eye max"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"keyterms": keyterms},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "speed": 1.15},
        },
    }


def _coerce_search(args: dict[str, Any]) -> dict[str, Any]:
    data = dict(args)
    allowed = {"any", "35mm", "imax", "70mm", "open_caption"}
    if data.get("format") not in allowed:
        data["format"] = "any"
    page = data.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > 10:
        data["page"] = 1
    location = data.get("location")
    if isinstance(location, str) and len(location) > 120:
        data["location"] = None
        data["_location_too_long"] = True
    return data


async def _execute(
    db: Database,
    name: str,
    args: dict[str, Any],
    *,
    live_data: bool = False,
    refresher: Any | None = None,
) -> dict[str, Any]:
    from pydantic import ValidationError

    from moviexfone.api.models import MovieIn, SearchIn
    from moviexfone.query import end_call, get_movie_info, search_showtimes

    now = now_ny()
    if name == "end_call":
        return end_call()
    if name == "get_movie_info":
        try:
            movie = MovieIn.model_validate(args).movie
        except ValidationError:
            return {"error": "movie_not_found", "speak": "Which movie?", "did_you_mean": []}
        return get_movie_info(db, movie)
    if name == "search_showtimes":
        raw = _coerce_search(args)
        if raw.pop("_location_too_long", False):
            return {
                "error": "location_not_found",
                "speak": "Which neighborhood or theater?",
            }
        try:
            body = SearchIn.model_validate(raw)
        except ValidationError:
            return {
                "error": "date_unclear",
                "speak": "Which day do you mean?",
                "note": "The arguments were not usable. Ask one short question.",
            }
        window = resolve_when(body.date, now)
        dates, spoken, unclear = window.dates, window.spoken, window.unclear
        after = body.after_time or (
            window.after.strftime("%H:%M") if window.after is not None else None
        )
        before = body.before_time or (
            window.before.strftime("%H:%M") if window.before is not None else None
        )
        if refresher is not None and body.movie:
            refresher.consider(body.movie)
        if not body.location:
            return {"error": "location_not_found", "speak": "Which neighborhood or theater?"}
        return search_showtimes(
            db,
            location=body.location,
            movie=body.movie,
            dates=dates,
            after_time=after,
            before_time=before,
            format_name=body.format,
            page=body.page,
            now=now,
            date_spoken=spoken,
            geocode=_geocode if live_data else None,
            date_unclear=unclear,
        )
    return {"error": "unknown_tool", "note": "Answer with what you have."}


def _client_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one realtime event onto browser messages. Audio is not dropped."""
    kind = event.get("type")
    if kind == "response.output_audio.delta":
        return [{"type": "audio", "data": event.get("delta") or ""}]
    if kind == "response.output_audio_transcript.delta":
        return [
            {
                "type": "transcript.delta",
                "role": "assistant",
                "text": event.get("delta") or "",
            }
        ]
    if kind == "input_audio_buffer.speech_started":
        return [{"type": "speech_started"}]
    return []


def _send_upstream(upstream: Any) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def send(message: dict[str, Any]) -> None:
        await upstream.send(json.dumps(message))

    return send
