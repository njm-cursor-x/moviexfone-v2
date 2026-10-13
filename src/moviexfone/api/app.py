"""FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from moviexfone.api.security import enforce_rate_limit, enforce_tool_secret
from moviexfone.catalog import seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.log import configure_logging
from moviexfone.query import end_call, get_movie_info, search_showtimes
from moviexfone.service import ask
from moviexfone.timeutil import now_ny, parse_date_token, upcoming_weekend
from moviexfone.voice.bridge import handle_voice_socket
from moviexfone.voice.prompt import GREETING, system_prompt
from moviexfone.voice.schemas import tool_schemas

log = logging.getLogger(__name__)

_FORMATS = {"any", "imax", "dolby", "3d", "70mm", "35mm"}


class SearchIn(BaseModel):
    location: str = Field(min_length=1, max_length=120)
    movie: str | None = Field(default=None, max_length=160)
    date: str | None = Field(default=None, max_length=40)
    after_time: str | None = Field(default=None, max_length=8)
    format: str = "any"
    page: int = Field(default=1, ge=1, le=10)

    @field_validator("location", "movie", "date", "after_time")
    @classmethod
    def no_nulls(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("invalid character")
        return value.strip() if isinstance(value, str) else value

    @field_validator("format")
    @classmethod
    def known_format(cls, value: str) -> str:
        if value not in _FORMATS:
            raise ValueError("invalid format")
        return value


class MovieIn(BaseModel):
    movie: str = Field(min_length=1, max_length=160)

    @field_validator("movie")
    @classmethod
    def no_nulls(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("invalid character")
        return value.strip()


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    page: int = Field(default=1, ge=1, le=10)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    secrets = [value for value in (settings.xai_api_key, settings.tool_secret) if value]
    configure_logging(secrets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        db = Database(settings.db_path)
        seed(db, settings.data_dir, now=now_ny())
        if settings.live_data:
            from moviexfone.catalog import refresh_live

            refresh_live(db, now=now_ny())
        app.state.db = db
        app.state.settings = settings
        from moviexfone.api.security import RateLimiter

        app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
        if settings.bind not in {"127.0.0.1", "localhost"} and not settings.tool_secret:
            log.warning("tool endpoints are open and the server is not bound to localhost")
        log.info("ready voice_mode=%s live_data=%s", settings.voice_mode, settings.live_data)
        yield
        db.close()

    app = FastAPI(title="MovieXFone", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-MovieXFone-Secret"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:"
        )
        return response

    static_dir = _static_dir()
    if static_dir is not None:
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def db_dep(request: Request) -> Database:
        database = request.app.state.db
        if not isinstance(database, Database):
            raise RuntimeError("database is not ready")
        return database

    def guarded(request: Request) -> None:
        enforce_rate_limit(request)
        enforce_tool_secret(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "voice_mode": settings.voice_mode,
            "live_data": settings.live_data,
            "now_ny": now_ny().isoformat(timespec="seconds"),
        }

    @app.get("/health/data")
    def health_data(db: Database = Depends(db_dep)) -> dict[str, Any]:
        counts = []
        for row in db.query(
            """SELECT source, COUNT(*) AS showtimes, COUNT(DISTINCT theater_id) AS theaters
               FROM showtime GROUP BY source"""
        ):
            counts.append(
                {
                    "source": row["source"],
                    "showtimes": row["showtimes"],
                    "theaters": row["theaters"],
                }
            )
        open_count = db.query_one("SELECT COUNT(*) AS n FROM theater WHERE status = 'open'")
        covered = db.query_one("SELECT COUNT(*) AS n FROM theater WHERE covered = 1")
        runs = [
            dict(row)
            for row in db.query(
                "SELECT source, started_at, ok, showtimes, error FROM ingest_run ORDER BY id DESC LIMIT 5"
            )
        ]
        return {
            "open_theaters": open_count["n"] if open_count else 0,
            "covered_theaters": covered["n"] if covered else 0,
            "sources": counts,
            "recent_runs": runs,
        }

    @app.post("/v1/tools/search_showtimes")
    def tool_search(
        body: SearchIn,
        _: None = Depends(guarded),
        db: Database = Depends(db_dep),
    ) -> JSONResponse:
        result = _search_from_body(db, body)
        return JSONResponse(result)

    @app.post("/v1/tools/get_movie_info")
    def tool_info(
        body: MovieIn,
        _: None = Depends(guarded),
        db: Database = Depends(db_dep),
    ) -> JSONResponse:
        return JSONResponse(get_movie_info(db, body.movie))

    @app.post("/v1/tools/end_call")
    def tool_end(_: None = Depends(guarded)) -> dict[str, Any]:
        return end_call()

    @app.get("/v1/builder/bundle")
    def builder_bundle() -> dict[str, Any]:
        now = now_ny()
        return {
            "prompt": system_prompt(now),
            "greeting": GREETING,
            "tools": tool_schemas(),
            "header": "X-MovieXFone-Secret",
            "endpoints": {
                "search_showtimes": "/v1/tools/search_showtimes",
                "get_movie_info": "/v1/tools/get_movie_info",
                "end_call": "/v1/tools/end_call",
            },
            "voice": {"id": "rex", "speed": 1.15},
            "notes": [
                "Call search_showtimes once per question.",
                "Batch is the Builder's job; our browser bridge also batches.",
                "Do not enable web_search on this agent.",
            ],
        }

    @app.post("/v1/demo/ask")
    def demo_ask(
        body: AskIn,
        request: Request,
        db: Database = Depends(db_dep),
    ) -> JSONResponse:
        enforce_rate_limit(request)
        return JSONResponse(ask(db, body.text, now=now_ny(), page=body.page))

    @app.post("/admin/refresh")
    def admin_refresh(request: Request, db: Database = Depends(db_dep)) -> dict[str, Any]:
        enforce_tool_secret(request)
        if (
            settings.tool_secret is None
            and request.client
            and request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        summary: dict[str, Any] = dict(seed(db, settings.data_dir, now=now_ny()))
        if settings.live_data:
            from moviexfone.catalog import refresh_live

            summary["live"] = refresh_live(db, now=now_ny())
        return {"ok": True, "reloaded_fixtures": True, **summary}

    @app.websocket("/ws/voice")
    async def voice(websocket: WebSocket) -> None:
        host = websocket.client.host if websocket.client else "unknown"
        if not websocket.app.state.limiter.allow(f"ws:{host}"):
            await websocket.close(code=1008)
            return
        await handle_voice_socket(websocket, settings, websocket.app.state.db)

    @app.get("/")
    def index() -> FileResponse:
        if static_dir is None:
            raise HTTPException(status_code=404, detail="demo page missing")
        return FileResponse(static_dir / "index.html")

    return app


def _search_from_body(db: Database, body: SearchIn) -> dict[str, Any]:
    now = now_ny()
    raw = (body.date or "today").strip()
    if "weekend" in raw.lower():
        dates = [day.isoformat() for day in upcoming_weekend(now.date())]
        spoken = "this weekend"
    else:
        parsed = parse_date_token(raw, now.date())
        if parsed is None and len(raw) == 10 and raw[4] == "-":
            dates = [raw]
            spoken = raw
        elif parsed is None:
            dates = [now.date().isoformat()]
            spoken = "today"
        else:
            dates = [parsed.isoformat()]
            lowered = raw.lower()
            if lowered == "tonight":
                spoken = "tonight"
            elif lowered == "today":
                spoken = "today"
            else:
                spoken = parsed.strftime("%A")
    return search_showtimes(
        db,
        location=body.location,
        movie=body.movie,
        dates=dates,
        after_time=body.after_time,
        format_name=body.format,
        page=body.page,
        now=now,
        date_spoken=spoken,
    )


def _static_dir() -> Path | None:
    candidates = [Path.cwd() / "static", Path(__file__).resolve().parents[3] / "static"]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None
