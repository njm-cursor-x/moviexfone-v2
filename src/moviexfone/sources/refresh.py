"""Background Assemble refresh. Slow city lookups stay off the request path.

The public API allows about 60 requests a minute and caches a city for about
15 minutes. This worker stays under that ceiling and only asks again when the
cache is stale.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta

from moviexfone.catalog import _apply, record_ingest
from moviexfone.db import Database
from moviexfone.http import AllowedClient
from moviexfone.sources.fetch import ALL_CITIES, fetch_assemble_city, lookup_film_id
from moviexfone.sources.fetch import _client as assemble_client
from moviexfone.sources.parse import parse_assemble
from moviexfone.timeutil import NY

log = logging.getLogger(__name__)

_MAX_PER_MINUTE = 50
_LOOKUP_MISS: dict[str, float] = {}


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


def assemble_is_fresh(
    db: Database, movie_id: str, *, now: datetime, ttl: timedelta | None = None
) -> bool:
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
    window = ttl or timedelta(seconds=900)
    return now.astimezone(NY) - fetched.astimezone(NY) < window


def refresh_assemble(
    db: Database,
    *,
    now: datetime,
    budget: RequestBudget | None = None,
    client: AllowedClient | None = None,
    only_title: str | None = None,
    ttl: timedelta | None = None,
) -> int:
    """Fetch NYC cities for one stale catalog film, or each stale film when asked."""
    limiter = budget or RequestBudget()
    own_client = client is None
    client = client or assemble_client(timeout=90.0)
    total = 0
    errors: list[str] = []
    fetched = False
    started = datetime.now(NY)
    movies = db.query("SELECT movie_id, title, tmdb_id FROM movie")
    try:
        for movie in movies:
            title = str(movie["title"])
            if only_title and title.lower() != only_title.lower():
                continue
            movie_id = str(movie["movie_id"])
            if assemble_is_fresh(db, movie_id, now=now, ttl=ttl):
                continue
            film_id = str(movie["tmdb_id"] or "")
            if not film_id:
                missed_at = _LOOKUP_MISS.get(movie_id)
                if missed_at is not None and time.monotonic() - missed_at < 900:
                    continue
                for _attempt in range(3):
                    limiter.wait()
                try:
                    found = lookup_film_id(title, client=client)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    found = None
                if not found:
                    _LOOKUP_MISS[movie_id] = time.monotonic()
                    continue
                film_id = found
                db.execute(
                    "UPDATE movie SET tmdb_id = ? WHERE movie_id = ?",
                    (film_id, movie_id),
                )
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
                fetched = True
        if fetched or errors:
            record_ingest(
                db,
                source="assemble",
                now=now,
                ok=not errors or total > 0,
                showtimes=total,
                error=errors[0] if errors and total == 0 else None,
                started=started,
                finished=datetime.now(NY),
            )
    finally:
        if own_client:
            client.close()
    return total


class AssembleRefresher:
    """Fetches one title when a search asks, plus one cold pass. No timer over every film."""

    def __init__(self, db: Database, *, ttl_seconds: int = 900) -> None:
        self._db = db
        self._ttl = timedelta(seconds=max(60, ttl_seconds))
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="assemble-refresh", daemon=True)

    def start(self) -> None:
        self._thread.start()
        for row in self._db.query(
            "SELECT title FROM movie WHERE tmdb_id IS NOT NULL AND tmdb_id != ''"
        ):
            self.consider(str(row["title"]))

    def consider(self, title: str) -> None:
        cleaned = title.strip()
        if cleaned:
            self._queue.put(cleaned)

    def stop(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        from moviexfone.timeutil import now_ny

        while True:
            title = self._queue.get()
            if title is None:
                return
            try:
                refresh_assemble(self._db, now=now_ny(), only_title=title, ttl=self._ttl)
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
