"""Background Assemble refresh. Slow city lookups stay off the request path.

The public API allows about 60 requests a minute and caches a city for about
15 minutes. This worker stays under that ceiling and only asks again when the
cache is stale.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from moviexfone.catalog import _apply, record_ingest
from moviexfone.db import Database
from moviexfone.http import AllowedClient
from moviexfone.sources.fetch import ALL_CITIES, fetch_assemble_city
from moviexfone.sources.fetch import _client as assemble_client
from moviexfone.sources.parse import parse_assemble
from moviexfone.timeutil import NY

log = logging.getLogger(__name__)

_MAX_PER_MINUTE = 50
_CACHE = timedelta(minutes=15)


class RequestBudget:
    """A sliding window that refuses to exceed the Assemble rate limit."""

    def __init__(self, limit: int = _MAX_PER_MINUTE, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._stamps = [stamp for stamp in self._stamps if now - stamp < self.window]
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                delay = self.window - (now - self._stamps[0])
            time.sleep(max(0.05, delay))


def assemble_is_fresh(db: Database, movie_id: str, *, now: datetime) -> bool:
    row = db.query_one(
        """SELECT MAX(fetched_at) AS fetched FROM showtime
           WHERE source = 'assemble' AND movie_id = ?""",
        (movie_id,),
    )
    if row is None or not row["fetched"]:
        return False
    fetched = datetime.fromisoformat(str(row["fetched"]))
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=NY)
    return now.astimezone(NY) - fetched.astimezone(NY) < _CACHE


def refresh_assemble(
    db: Database,
    *,
    now: datetime,
    budget: RequestBudget | None = None,
    client: AllowedClient | None = None,
) -> int:
    """Fetch NYC cities for catalog films that have a TMDB id and a stale cache."""
    limiter = budget or RequestBudget()
    own_client = client is None
    client = client or assemble_client()
    total = 0
    errors: list[str] = []
    movies = db.query(
        "SELECT movie_id, title, tmdb_id FROM movie WHERE tmdb_id IS NOT NULL AND tmdb_id != ''"
    )
    try:
        for movie in movies:
            movie_id = str(movie["movie_id"])
            if assemble_is_fresh(db, movie_id, now=now):
                continue
            film_id = str(movie["tmdb_id"])
            title = str(movie["title"])
            cleared = False
            for slug in ALL_CITIES:
                limiter.wait()
                try:
                    payload = fetch_assemble_city(film_id, slug, client=client)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    log.warning("assemble refresh failed: %s", type(exc).__name__)
                    continue
                rows = parse_assemble(payload, title=title)
                if rows and not cleared:
                    db.execute(
                        "DELETE FROM showtime WHERE source = 'assemble' AND movie_id = ?",
                        (movie_id,),
                    )
                    cleared = True
                total += _apply(db, rows, mode="insert", now=now)
        record_ingest(
            db,
            source="assemble",
            now=now,
            ok=not errors or total > 0,
            showtimes=total,
            error=errors[0] if errors and total == 0 else None,
        )
    finally:
        if own_client:
            client.close()
    return total


class AssembleRefresher:
    """Daemon thread. One refresh, then every 15 minutes until stopped."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="assemble-refresh", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                from moviexfone.timeutil import now_ny

                refresh_assemble(self._db, now=now_ny())
            except Exception as exc:
                log.warning("assemble worker failed: %s", type(exc).__name__)
                record_ingest(
                    self._db,
                    source="assemble",
                    now=datetime.now(NY),
                    ok=False,
                    showtimes=0,
                    error=type(exc).__name__,
                )
            self._stop.wait(iso_wait())


def iso_wait() -> float:
    return 15 * 60
