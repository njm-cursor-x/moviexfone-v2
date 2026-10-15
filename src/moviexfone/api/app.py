"""FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from moviexfone.api.models import AskIn, MovieIn, SearchIn
from moviexfone.api.security import (
    SessionCaps,
    VoiceTickets,
    basic_auth_ok,
    enforce_deployment,
    enforce_rate_limit,
    enforce_tool_secret,
    local_hosts,
    origin_allowed,
    secrets_equal,
    ticket_allowed,
)
from moviexfone.catalog import seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.log import configure_logging
from moviexfone.query import end_call, get_movie_info, search_showtimes
from moviexfone.service import ask
from moviexfone.sources.fetch import geosearch
from moviexfone.sources.refresh import AssembleRefresher, PopulationScheduler
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
        # Live startup loads the registry only. Fixture showtimes stay in the
        # offline tests and in an explicit MOVIEXFONE_LIVE_DATA=0 run.
        seed(db, settings.data_dir, now=now_ny(), include_showtimes=not settings.live_data)
        from moviexfone.sources.refresh import GLOBAL_BUDGET

        GLOBAL_BUDGET.attach(db)
        refresher: AssembleRefresher | None = None
        scheduler: PopulationScheduler | None = None
        if settings.live_data:
            from moviexfone.catalog import refresh_live

            # Same fetches as populate_live. Alamo and Nitehawk finish before
            # the port opens. Assemble stays on the worker so a cold crawl
            # does not hold the listen socket. The schedule calls populate_live.
            refresh_live(db, now=now_ny())
            refresher = AssembleRefresher(db, ttl_seconds=settings.assemble_ttl_seconds)
            refresher.start()
            scheduler = PopulationScheduler(db)
            scheduler.start()
        app.state.db = db
        app.state.settings = settings
        app.state.refresher = refresher
        app.state.schedule = scheduler
        from moviexfone.api.security import RateLimiter

        app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
        app.state.ticket_limiter = RateLimiter(5)
        app.state.sessions = SessionCaps(
            db,
            daily=settings.daily_sessions,
            allow_hosts=settings.session_allow_hosts,
        )
        app.state.tickets = VoiceTickets()
        log.info("ready voice_mode=%s live_data=%s", settings.voice_mode, settings.live_data)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            if refresher is not None:
                refresher.stop()
            db.close()

    app = FastAPI(title="MovieXFone", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def basic_gate(request: Request, call_next: Any) -> Response:
        path = request.url.path
        gated = path in {
            "/",
            "/v1/voice/ticket",
            "/health/data",
            "/v1/builder/bundle",
        } or path.startswith("/static")
        if (
            gated
            and settings.basic_user
            and not basic_auth_ok(
                request.headers.get("authorization"),
                settings.basic_user,
                settings.basic_password,
            )
        ):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="MovieXFone"'},
            )
        response: Response = await call_next(request)
        return response

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

    @app.post("/v1/voice/ticket")
    def voice_ticket(request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        host = request.client.host if request.client else None
        if not request.app.state.ticket_limiter.allow(host or "unknown"):
            raise HTTPException(
                status_code=429, detail="Too many requests", headers={"Retry-After": "60"}
            )
        if not ticket_allowed(
            origin=origin,
            host=host,
            header_secret=request.headers.get("x-moviexfone-secret"),
            allowed_origins=settings.cors_origins,
            secret=settings.tool_secret,
            authorization=request.headers.get("authorization"),
            basic_user=settings.basic_user,
            basic_password=settings.basic_password,
        ):
            raise HTTPException(status_code=401, detail="not authorized")
        return {"ticket": request.app.state.tickets.issue(), "expires_in": 60}

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
    def builder_bundle(_: None = Depends(guarded)) -> dict[str, Any]:
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
            from moviexfone.catalog import populate_live

            report = populate_live(db, now=now_ny())
            summary["assemble"] = report.pop("assemble")
            summary["live"] = report
        return {"ok": True, "reloaded_fixtures": not settings.live_data, **summary}

    @app.websocket("/ws/voice")
    async def voice(websocket: WebSocket) -> None:
        host = websocket.client.host if websocket.client else "unknown"
        if settings.basic_user and not basic_auth_ok(
            websocket.headers.get("authorization"),
            settings.basic_user,
            settings.basic_password,
        ):
            await websocket.close(code=1008)
            return
        if not websocket.app.state.limiter.allow(f"ws:{host}"):
            await websocket.close(code=1008)
            return
        blocked = websocket.app.state.sessions.hold(host)
        if blocked:
            await websocket.accept()
            await websocket.send_json({"type": "error", "message": blocked})
            await websocket.close(code=1013)
            return
        try:
            await handle_voice_socket(
                websocket,
                settings,
                websocket.app.state.db,
                refresher=getattr(websocket.app.state, "refresher", None),
                tickets=getattr(websocket.app.state, "tickets", None),
                sessions=websocket.app.state.sessions,
                client_host=host,
            )
        finally:
            websocket.app.state.sessions.release(host)

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
    window = resolve_when(body.date, now)
    dates, spoken, unclear = window.dates, window.spoken, window.unclear
    after = body.after_time or (window.after.strftime("%H:%M") if window.after else None)
    before = body.before_time or (window.before.strftime("%H:%M") if window.before else None)
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
    origin = request.headers.get("origin")
    test_origin = (
        host == "testclient" and origin is not None and origin.startswith("http://testserver")
    )
    if origin and not origin_allowed(origin, settings.cors_origins) and not test_origin:
        raise HTTPException(status_code=403, detail="origin not allowed")
    if host in local_hosts():
        return
    secret = settings.tool_secret or ""
    got = request.headers.get("x-moviexfone-secret", "")
    if secret and secrets_equal(got, secret):
        return
    raise HTTPException(status_code=403, detail="origin not allowed")


def _static_dir() -> Path | None:
    candidates = [Path.cwd() / "static", Path(__file__).resolve().parents[3] / "static"]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None
