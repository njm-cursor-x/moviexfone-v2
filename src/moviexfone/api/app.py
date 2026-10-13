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

from moviexfone.api.models import AskIn, MovieIn, SearchIn
from moviexfone.api.security import (
    enforce_deployment,
    enforce_rate_limit,
    enforce_tool_secret,
    local_hosts,
    voice_connection_allowed,
)
from moviexfone.catalog import seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.log import configure_logging
from moviexfone.query import end_call, get_movie_info, search_showtimes
from moviexfone.service import ask
from moviexfone.sources.fetch import geosearch
from moviexfone.sources.refresh import AssembleRefresher, refresh_assemble
from moviexfone.timeutil import now_ny, resolve_when
from moviexfone.voice.bridge import handle_voice_socket
from moviexfone.voice.prompt import GREETING, system_prompt
from moviexfone.voice.schemas import tool_schemas

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    enforce_deployment(settings)
    secrets = [value for value in (settings.xai_api_key, settings.tool_secret) if value]
    configure_logging(secrets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        db = Database(settings.db_path)
        seed(db, settings.data_dir, now=now_ny(), include_showtimes=not settings.live_data)
        refresher: AssembleRefresher | None = None
        if settings.live_data:
            from moviexfone.catalog import refresh_live

            refresh_live(db, now=now_ny())
            refresher = AssembleRefresher(db, ttl_seconds=settings.assemble_ttl_seconds)
            refresher.start()
        app.state.db = db
        app.state.settings = settings
        app.state.refresher = refresher
        from moviexfone.api.security import RateLimiter

        app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
        log.info("ready voice_mode=%s live_data=%s", settings.voice_mode, settings.live_data)
        try:
            yield
        finally:
            if refresher is not None:
                refresher.stop()
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
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
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
                """SELECT source, started_at, finished_at, ok, showtimes, error
                   FROM ingest_run ORDER BY id DESC LIMIT 20"""
            )
        ]
        latest: dict[str, dict[str, Any]] = {}
        for run in reversed(runs):
            latest[str(run["source"])] = run
        by_source = {str(row["source"]): row for row in counts}
        return {
            "open_theaters": open_count["n"] if open_count else 0,
            "covered_theaters": covered["n"] if covered else 0,
            "sources": counts,
            "recent_runs": runs[:8],
            "by_source": {
                name: {
                    "showtimes": by_source.get(name, {}).get("showtimes", 0),
                    "theaters": by_source.get(name, {}).get("theaters", 0),
                    "last_fetch_at": run.get("finished_at") or run.get("started_at"),
                    "last_ok": bool(run.get("ok")),
                    "last_error": run.get("error"),
                }
                for name, run in latest.items()
            },
        }

    @app.post("/v1/tools/search_showtimes")
    def tool_search(
        body: SearchIn,
        _: None = Depends(guarded),
        db: Database = Depends(db_dep),
    ) -> JSONResponse:
        result = _search_from_body(db, body, settings)
        refresher = getattr(app.state, "refresher", None)
        if refresher is not None and body.movie:
            refresher.consider(body.movie)
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
        _require_demo_access(request, settings)
        return JSONResponse(
            ask(db, body.text, now=now_ny(), page=body.page, geocode=geocode_for(settings))
        )

    @app.post("/admin/refresh")
    def admin_refresh(request: Request, db: Database = Depends(db_dep)) -> dict[str, Any]:
        enforce_rate_limit(request)
        enforce_tool_secret(request)
        host = request.client.host if request.client else None
        if settings.tool_secret is None and host not in local_hosts():
            raise HTTPException(status_code=401, detail="unauthorized")
        summary: dict[str, Any] = dict(
            seed(db, settings.data_dir, now=now_ny(), include_showtimes=not settings.live_data)
        )
        if settings.live_data:
            from moviexfone.catalog import refresh_live

            summary["live"] = refresh_live(db, now=now_ny())
            summary["assemble"] = refresh_assemble(db, now=now_ny())
        return {"ok": True, "reloaded_fixtures": not settings.live_data, **summary}

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


def _search_from_body(db: Database, body: SearchIn, settings: Settings) -> dict[str, Any]:
    now = now_ny()
    if not body.location:
        return {
            "error": "location_not_found",
            "speak": "Which neighborhood or theater?",
            "ask": "Which neighborhood or theater?",
        }
    dates, spoken, unclear = resolve_when(body.date, now)
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
        geocode=geocode_for(settings),
        date_unclear=unclear,
    )


def _geocode(text: str) -> dict[str, Any] | None:
    try:
        return geosearch(text)
    except Exception as exc:
        log.info("geosearch failed: %s", type(exc).__name__)
        return None


def geocode_for(settings: Settings) -> Any:
    if not settings.live_data:
        return None
    return _geocode


def _require_demo_access(request: Request, settings: Settings) -> None:
    host = request.client.host if request.client else None
    token = request.headers.get("x-moviexfone-secret")
    if voice_connection_allowed(
        origin=request.headers.get("origin"),
        host=host,
        token=token,
        allowed_origins=settings.cors_origins,
        secret=settings.tool_secret,
    ):
        return
    raise HTTPException(status_code=403, detail="origin not allowed")


def _static_dir() -> Path | None:
    candidates = [Path.cwd() / "static", Path(__file__).resolve().parents[3] / "static"]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None
