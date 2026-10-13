"""WebSocket bridge between the browser and xAI realtime, plus a mock mode."""

from __future__ import annotations

import asyncio
import json
import logging
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


async def handle_voice_socket(websocket: WebSocket, settings: Settings, db: Database) -> None:
    await websocket.accept()
    host = websocket.client.host if websocket.client else None
    if not voice_connection_allowed(
        origin=websocket.headers.get("origin"),
        host=host,
        token=websocket.query_params.get("token"),
        allowed_origins=settings.cors_origins,
        secret=settings.tool_secret,
    ):
        await websocket.send_json({"type": "error", "message": "origin not allowed"})
        await websocket.close(code=1008)
        return
    if settings.voice_mode == "live":
        try:
            await _run_live(websocket, settings, db)
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


async def _run_live(websocket: WebSocket, settings: Settings, db: Database) -> None:
    import websockets

    if not settings.xai_api_key:
        raise RuntimeError("missing key")
    url = f"wss://api.x.ai/v1/realtime?model={settings.xai_model}"
    headers = {"Authorization": f"Bearer {settings.xai_api_key}"}
    async with websockets.connect(
        url, additional_headers=headers, max_size=None, open_timeout=20
    ) as upstream:
        await upstream.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": _session_body(db),
                }
            )
        )
        while True:
            raw = await upstream.recv()
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            if event.get("type") == "session.updated":
                break
            if event.get("type") == "error":
                raise RuntimeError("session update was rejected")
        await websocket.send_json({"type": "session.ready", "mode": "live", "greeting": GREETING})
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
        await websocket.send_json({"type": "session.ready", "mode": "live", "greeting": GREETING})
        loop = ToolLoop(
            lambda name, args: _execute(db, name, args),
            send=_send_upstream(upstream),
        )

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "audio":
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
                    await websocket.send_json({"type": "audio", "data": event.get("delta") or ""})
                elif kind == "response.output_audio_transcript.delta":
                    await websocket.send_json(
                        {
                            "type": "transcript.delta",
                            "role": "assistant",
                            "text": event.get("delta") or "",
                        }
                    )
                elif kind == "conversation.item.input_audio_transcription.completed":
                    await websocket.send_json(
                        {
                            "type": "transcript",
                            "role": "user",
                            "text": event.get("transcript") or "",
                        }
                    )
                elif kind == "input_audio_buffer.speech_started":
                    await websocket.send_json({"type": "speech_started"})
                elif kind == "response.done":
                    await websocket.send_json({"type": "transcript.done", "role": "assistant"})
                elif kind == "error":
                    await websocket.send_json(
                        {"type": "error", "message": "The voice service returned an error."}
                    )
                    log.warning("realtime error type=%s", kind)
                outbound = await loop.handle_server_event(event)
                for item in outbound:
                    await upstream.send(json.dumps(item))
                    output = item.get("item")
                    if isinstance(output, dict) and output.get("type") == "function_call_output":
                        try:
                            payload = json.loads(str(output.get("output") or "{}"))
                        except json.JSONDecodeError:
                            payload = {}
                        if isinstance(payload, dict) and payload.get("speak"):
                            await websocket.send_json({"type": "showtimes", "payload": payload})

        client_task = asyncio.create_task(client_to_upstream())
        upstream_task = asyncio.create_task(upstream_to_client())
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
        finally:
            for task in (client_task, upstream_task, limit):
                task.cancel()
            await asyncio.gather(client_task, upstream_task, limit, return_exceptions=True)
            await upstream.close()


def _session_body(db: Database) -> dict[str, Any]:
    titles = [str(row["title"]) for row in db.query("SELECT title FROM movie ORDER BY title")]
    theaters = [
        str(row["spoken_name"])
        for row in db.query(
            "SELECT spoken_name FROM theater WHERE status = 'open' ORDER BY spoken_name LIMIT 40"
        )
    ]
    keyterms = [term[:50] for term in [*titles, *theaters] if term][:100]
    return {
        "voice": "rex",
        "instructions": system_prompt(now_ny()),
        "turn_detection": {"type": "server_vad", "idle_timeout_ms": 120000},
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


async def _execute(db: Database, name: str, args: dict[str, Any]) -> dict[str, Any]:
    from moviexfone.query import end_call, get_movie_info, search_showtimes

    now = now_ny()
    if name == "end_call":
        return end_call()
    if name == "get_movie_info":
        return get_movie_info(db, str(args.get("movie") or ""))
    if name == "search_showtimes":
        raw_date = args.get("date")
        dates, spoken, unclear = resolve_when(None if raw_date is None else str(raw_date), now)
        raw_format = args.get("format")
        raw_page = args.get("page")
        raw_movie = args.get("movie")
        location = args.get("location")
        if not location:
            return {
                "error": "location_not_found",
                "speak": "Which neighborhood or theater?",
            }
        return search_showtimes(
            db,
            location=str(location),
            movie=None if raw_movie in (None, "") else str(raw_movie),
            dates=dates,
            after_time=None
            if args.get("after_time") in (None, "")
            else str(args.get("after_time")),
            format_name="any" if raw_format in (None, "") else str(raw_format).lower(),
            page=1 if raw_page in (None, "") else int(raw_page),
            now=now,
            date_spoken=spoken,
            geocode=_geocode,
            date_unclear=unclear,
        )
    return {"error": "unknown_tool", "note": "Answer with what you have."}


def _send_upstream(upstream: Any) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def send(message: dict[str, Any]) -> None:
        await upstream.send(json.dumps(message))

    return send
