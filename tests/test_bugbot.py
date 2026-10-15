"""Regressions for the Bugbot review. No live HTTP."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from moviexfone.api.app import create_app
from moviexfone.catalog import _apply, load_theater_refs
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.query import search_showtimes
from moviexfone.service import ask
from moviexfone.sources.fetch import AssembleCityError, fetch_assemble_city
from moviexfone.sources.parse import IncomingShow
from moviexfone.sources.refresh import RequestBudget, refresh_assemble
from moviexfone.timeutil import NY
from moviexfone.voice.bridge import _coerce_search, _execute, _session_body
from tests.clock import NOW

BROOKLYN = "alamo-drafthouse-downtown-brooklyn"
MANHATTAN = "amc-empire-25"
QUEENS = "amc-bay-terrace-6"


def _show(theater: str, starts: datetime) -> IncomingShow:
    return IncomingShow(
        theater_key=theater,
        movie_title="Verity",
        starts_at=starts,
        format_norm="unknown",
        format_label=None,
        status="on_sale",
        ticket_url="https://example.test/tickets",
        source="assemble",
    )


class _Response:
    def __init__(self, code: int, body: dict[str, object] | None = None) -> None:
        self.status_code = code
        self.headers: dict[str, str] = {}
        self._body = body if body is not None else {}

    def json(self) -> dict[str, object]:
        return self._body


class _Client:
    def __init__(self, code: int) -> None:
        self.code = code

    def get(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response(self.code)


def test_non_200_assemble_city_is_not_an_empty_listing() -> None:
    try:
        fetch_assemble_city("1283515", "brooklyn-ny", client=_Client(500))  # type: ignore[arg-type]
        raised = False
    except AssembleCityError as exc:
        raised = True
        assert exc.city_slug == "brooklyn-ny"
        assert exc.status == 500
    assert raised
    assert fetch_assemble_city("1283515", "brooklyn-ny", client=_Client(200)) == {}  # type: ignore[arg-type]


def test_failed_assemble_city_keeps_its_rows(db: Database, monkeypatch: object) -> None:
    _apply(
        db,
        [
            _show(BROOKLYN, datetime(2026, 10, 16, 11, 11, tzinfo=NY)),
            _show(QUEENS, datetime(2026, 10, 16, 11, 22, tzinfo=NY)),
            _show(MANHATTAN, datetime(2026, 10, 16, 18, 0, tzinfo=NY)),
        ],
        mode="insert",
        now=NOW,
    )

    def fake_city(
        _film: str, slug: str, *, client: object = None, on_retry: object = None
    ) -> dict[str, object]:
        if slug in {"brooklyn-ny", "flushing-ny"}:
            raise AssembleCityError(slug, 500)
        if slug == "new-york-ny":
            return {
                "film_id": "1283515",
                "screenings": [
                    {
                        "venue": {"name": "AMC Empire 25"},
                        "starts_at": "2026-10-14T21:00:00-04:00",
                        "availability": "on_sale",
                        "url": "https://www.amctheatres.com/showtimes",
                    }
                ],
            }
        return {}

    monkeypatch.setattr("moviexfone.sources.refresh.fetch_assemble_city", fake_city)  # type: ignore[attr-defined]
    refresh_assemble(
        db,
        now=NOW + timedelta(hours=3),
        budget=RequestBudget(),
        only_title="Verity",
    )
    rows = db.query(
        """SELECT theater_id, starts_at FROM showtime
           WHERE source = 'assemble' AND movie_id = 'movie:verity'"""
    )
    stamps = {(str(row["theater_id"]), str(row["starts_at"])) for row in rows}
    assert (BROOKLYN, "2026-10-16T11:11:00-04:00") in stamps
    assert (QUEENS, "2026-10-16T11:22:00-04:00") in stamps
    assert (MANHATTAN, "2026-10-16T18:00:00-04:00") not in stamps
    assert any(theater == MANHATTAN and "21:00" in stamp for theater, stamp in stamps)
    status = db.query_one(
        "SELECT checked_at FROM assemble_status WHERE movie_id = ?",
        ("movie:verity",),
    )
    assert status is None
    run = db.query_one(
        "SELECT ok FROM ingest_run WHERE source = 'assemble' ORDER BY id DESC LIMIT 1"
    )
    assert run is not None and int(run["ok"]) == 0


def test_coerce_search_keeps_dolby_and_3d(db: Database) -> None:
    assert _coerce_search({"format": "dolby", "location": "Film Forum"})["format"] == "dolby"
    assert _coerce_search({"format": "3d", "location": "Film Forum"})["format"] == "3d"
    assert _coerce_search({"format": "laser", "location": "Film Forum"})["format"] == "any"
    spoken = asyncio.run(
        _execute(
            db,
            "search_showtimes",
            {"location": "Williamsburg", "format": "dolby", "date": "today"},
        )
    )
    assert "Dolby" in spoken["speak"]


def test_typed_zip_still_searches(db: Database) -> None:
    result = ask(db, "What's playing in 11211?", now=NOW)
    assert result["parsed"]["location"] == "11211"
    assert result["resolved_location"]["label"] == "Williamsburg"
    assert "Which neighborhood" not in result["speak"]


def test_showtime_search_is_bounded(db: Database) -> None:
    seen: list[str] = []
    original = db.query

    def capture(sql: str, params: tuple[object, ...] = ()) -> list[object]:
        if "FROM showtime" in sql and "spoken_name" in sql:
            seen.append(" ".join(sql.split()))
        return original(sql, params)  # type: ignore[arg-type]

    db.query = capture  # type: ignore[method-assign]
    search_showtimes(
        db,
        location="Alamo Drafthouse Downtown Brooklyn",
        movie="Verity",
        dates=["2026-10-13"],
        now=NOW,
        date_spoken="today",
    )
    assert seen
    folded = seen[0].lower()
    assert "starts_at >=" in folded
    assert "movie_id = ?" in folded
    assert "theater_id in" in folded
    assert "business_date in" in folded

    seen.clear()
    search_showtimes(
        db,
        location="Williamsburg",
        dates=["2026-10-13"],
        now=NOW,
        date_spoken="today",
    )
    assert seen
    folded = seen[0].lower()
    assert "starts_at >=" in folded
    assert "between" in folded
    assert "business_date in" in folded
    assert "theater_id in" not in folded


def test_session_titles_are_capped_and_registry_is_complete(db: Database) -> None:
    seen: list[str] = []
    original = db.query

    def capture(sql: str, params: tuple[object, ...] = ()) -> list[object]:
        seen.append(sql)
        return original(sql, params)  # type: ignore[arg-type]

    db.query = capture  # type: ignore[method-assign]
    _session_body(db)
    title_sql = [sql for sql in seen if "FROM movie" in sql]
    assert title_sql
    assert "LIMIT 100" in title_sql[0]
    db.query = original  # type: ignore[method-assign]
    count = db.query_one("SELECT COUNT(*) AS n FROM theater")
    assert count is not None
    assert len(load_theater_refs(db)) == int(count["n"])


def _settings(base: Settings, **overrides: object) -> Settings:
    values = {
        "xai_api_key": base.xai_api_key,
        "live_voice": False,
        "live_data": False,
        "tool_secret": base.tool_secret,
        "db_path": base.db_path,
        "data_dir": base.data_dir,
        "bind": base.bind,
        "port": base.port,
        "cors_origins": base.cors_origins,
        "rate_limit_per_minute": base.rate_limit_per_minute,
        "assemble_ttl_seconds": base.assemble_ttl_seconds,
        "xai_model": base.xai_model,
        "max_session_seconds": base.max_session_seconds,
        "basic_user": base.basic_user,
        "basic_password": base.basic_password,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_builder_bundle_requires_the_tool_secret(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        denied = client.get("/v1/builder/bundle")
        assert denied.status_code == 401
        allowed = client.get(
            "/v1/builder/bundle",
            headers={"X-MovieXFone-Secret": "test-secret"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["header"] == "X-MovieXFone-Secret"


def test_builder_bundle_localhost_stays_open_without_a_secret(settings: Settings) -> None:
    opened = _settings(settings, tool_secret=None, basic_user=None, basic_password=None)
    with TestClient(create_app(opened)) as client:
        assert client.get("/v1/builder/bundle").status_code == 200


def test_builder_bundle_remote_host_requires_a_credential(
    settings: Settings, monkeypatch: object
) -> None:
    monkeypatch.setattr("moviexfone.api.security.local_hosts", lambda: set())  # type: ignore[attr-defined]
    remote = _settings(settings, tool_secret=None, basic_user=None, basic_password=None)
    with TestClient(create_app(remote)) as client:
        assert client.get("/v1/builder/bundle").status_code == 401


def test_builder_bundle_honors_basic_auth(settings: Settings) -> None:
    locked = _settings(
        settings,
        tool_secret="test-secret",
        basic_user="mx",
        basic_password="board-secret",
    )
    token = base64.b64encode(b"mx:board-secret").decode("ascii")
    with TestClient(create_app(locked)) as client:
        denied = client.get(
            "/v1/builder/bundle",
            headers={"X-MovieXFone-Secret": "test-secret"},
        )
        assert denied.status_code == 401
        allowed = client.get(
            "/v1/builder/bundle",
            headers={
                "X-MovieXFone-Secret": "test-secret",
                "Authorization": f"Basic {token}",
            },
        )
        assert allowed.status_code == 200


def test_healthcheck_sets_a_timeout() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" in text
