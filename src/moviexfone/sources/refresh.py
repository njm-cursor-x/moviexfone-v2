"""Background Assemble refresh, and the weekday population schedule.

The public API allows about 60 requests a minute and caches a city for about
15 minutes. This worker stays under that ceiling and only asks again when the
cache is stale. The bucket is per process: run one process, or one uvicorn
worker. Several workers each keep their own bucket.

Full population (Alamo, Nitehawk, Assemble) runs at startup and on Monday,
Thursday, and Saturday at 07:00 America/New_York. There is no 45-minute timer.
A slot that passes while the process is down is covered by the next startup.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from moviexfone.catalog import _apply, record_ingest
from moviexfone.db import Database
from moviexfone.http import AllowedClient
from moviexfone.sources.fetch import (
    ALL_CITIES,
    city_slug_for_theater,
    fetch_assemble_city,
    lookup_film_id,
)
from moviexfone.sources.fetch import _client as assemble_client
from moviexfone.sources.parse import IncomingShow, parse_assemble
from moviexfone.timeutil import NY, now_ny

log = logging.getLogger(__name__)

_MAX_PER_MINUTE = 50
_LOOKUP_MISS: dict[str, float] = {}


class RequestBudget:
    """A sliding window that refuses to exceed the Assemble rate limit.

    Stamps are wall-clock seconds. When a database is attached, they are reloaded
    at startup so a restart inside the same minute does not open a new window.
    """

    def __init__(self, limit: int = _MAX_PER_MINUTE, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._stamps: list[float] = []
        self._lock = threading.Lock()
        self._db: Database | None = None

    def attach(self, db: Database) -> None:
        with self._lock:
            self._db = db
            now = time.time()
            rows = db.query(
                "SELECT sent_at FROM assemble_budget WHERE sent_at >= ? ORDER BY sent_at",
                (now - self.window,),
            )
            self._stamps = [float(row["sent_at"]) for row in rows]

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.time()
                self._stamps = [stamp for stamp in self._stamps if now - stamp < self.window]
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    if self._db is not None:
                        self._db.execute(
                            "INSERT INTO assemble_budget (sent_at) VALUES (?)",
                            (now,),
                        )
                        self._db.execute(
                            "DELETE FROM assemble_budget WHERE sent_at < ?",
                            (now - self.window,),
                        )
                    return
                delay = self.window - (now - self._stamps[0])
            time.sleep(max(0.05, delay))


# One bucket for warm-up, consider(), and /admin/refresh.
GLOBAL_BUDGET = RequestBudget(limit=_MAX_PER_MINUTE)


def _theaters_by_city(db: Database) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {slug: [] for slug in ALL_CITIES}
    for row in db.query("SELECT theater_id, borough, neighborhood, lat, lng FROM theater"):
        lat = row["lat"]
        lng = row["lng"]
        slug = city_slug_for_theater(
            str(row["borough"] or ""),
            str(row["neighborhood"] or ""),
            None if lat is None else float(lat),
            None if lng is None else float(lng),
        )
        if slug in grouped:
            grouped[slug].append(str(row["theater_id"]))
    return grouped


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
    limiter = budget if budget is not None else GLOBAL_BUDGET
    own_client = client is None
    client = client or assemble_client(timeout=90.0)
    total = 0
    errors: list[str] = []
    fetched = False
    city_failed = False
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
                from moviexfone.catalog import film_lookup_blocked, remember_film_miss

                if film_lookup_blocked(db, title, now):
                    continue
                missed_at = _LOOKUP_MISS.get(movie_id)
                if missed_at is not None and time.monotonic() - missed_at < 900:
                    continue
                limiter.wait()
                failed = False
                try:
                    found = lookup_film_id(title, client=client)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    found = None
                    failed = True
                if not found:
                    if failed:
                        _LOOKUP_MISS[movie_id] = time.monotonic()
                    else:
                        remember_film_miss(db, title, now)
                    continue
                film_id = found
                db.execute(
                    "UPDATE movie SET tmdb_id = ? WHERE movie_id = ?",
                    (film_id, movie_id),
                )
                from moviexfone.catalog import remember_film_ids

                remember_film_ids(db)
            collected: list[IncomingShow] = []
            failed_slugs: set[str] = set()
            succeeded_slugs: list[str] = []
            for slug in ALL_CITIES:
                limiter.wait()
                try:
                    payload = fetch_assemble_city(
                        film_id, slug, client=client, on_retry=limiter.wait
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    failed_slugs.add(slug)
                    log.warning("assemble refresh failed: %s", type(exc).__name__)
                    continue
                succeeded_slugs.append(slug)
                collected.extend(parse_assemble(payload, title=title))
            with db.transaction():
                # A 200 is that city's listing, including an empty body, so only
                # its theaters are replaced. A non-200 or transport error is not
                # an empty listing: that city keeps its rows. Queens slugs are
                # independent, so one answer is not held for the other two.
                # The film stays unchecked until every city answers.
                if failed_slugs:
                    city_failed = True
                by_city = _theaters_by_city(db)
                for slug in succeeded_slugs:
                    for theater_id in by_city[slug]:
                        db.execute(
                            """DELETE FROM showtime
                               WHERE source = 'assemble' AND movie_id = ? AND theater_id = ?""",
                            (movie_id, theater_id),
                        )
                total += _apply(db, collected, mode="insert", now=now)
            if not failed_slugs:
                fetched = True
                db.execute(
                    "INSERT OR REPLACE INTO assemble_status (movie_id, checked_at) VALUES (?, ?)",
                    (movie_id, started.isoformat()),
                )
        if fetched or errors:
            record_ingest(
                db,
                source="assemble",
                now=now,
                ok=False if city_failed else (not errors or total > 0),
                showtimes=total,
                error=errors[0] if errors and (city_failed or total == 0) else None,
                started=started,
                finished=datetime.now(NY),
            )
    finally:
        if own_client:
            client.close()
    return total


# Monday, Thursday, Saturday.
_POPULATION_WEEKDAYS = frozenset({0, 3, 5})


def next_population_at(now: datetime) -> datetime:
    """Next Monday, Thursday, or Saturday at 07:00 America/New_York strictly after `now`.

    A slot equal to `now` is not returned. Startup already populated, and a
    process that was down at 07:00 catches up on the next start, not by replaying
    the missed slot here.
    """
    local = now.astimezone(NY)
    day = local.date()
    for offset in range(8):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() not in _POPULATION_WEEKDAYS:
            continue
        slot = datetime(candidate.year, candidate.month, candidate.day, 7, 0, tzinfo=NY)
        if slot > local:
            return slot
    raise RuntimeError("no population slot in the next week")


def run_population(db: Database, *, now: datetime) -> dict[str, object]:
    from moviexfone.catalog import populate_live

    return populate_live(db, now=now)


class PopulationScheduler:
    """Runs startup's population on Monday, Thursday, and Saturday at 07:00 NY.

    It only fires while this process is up. A missed slot is not replayed.
    """

    def __init__(
        self,
        db: Database,
        *,
        populate: Callable[..., dict[str, object]] | None = None,
        now: Callable[[], datetime] | None = None,
        wait: Callable[[float], bool] | None = None,
        max_sleep: float = 30.0,
    ) -> None:
        self._db = db
        self._populate = populate if populate is not None else run_population
        self._now = now if now is not None else now_ny
        self._wait_fn = wait
        self._max_sleep = max_sleep
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="population-schedule", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _wait(self, seconds: float) -> bool:
        if self._wait_fn is not None:
            return self._wait_fn(seconds)
        return self._stop.wait(seconds)

    def _sleep_until(self, target: datetime) -> bool:
        """Return True when `target` has arrived. Return False when stopping."""
        while not self._stop.is_set():
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return True
            step = remaining if self._wait_fn is not None else min(self._max_sleep, remaining)
            if self._wait(step):
                return False
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            target = next_population_at(self._now())
            if not self._sleep_until(target):
                return
            if self._stop.is_set():
                return
            try:
                self._populate(self._db, now=self._now())
            except Exception as exc:
                log.warning("scheduled population failed: %s", type(exc).__name__)


class AssembleRefresher:
    """Fetches one title when a search asks, plus one cold pass. No timer over every film."""

    def __init__(self, db: Database, *, ttl_seconds: int = 900) -> None:
        self._db = db
        self._ttl = timedelta(seconds=max(60, ttl_seconds))
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="assemble-refresh", daemon=True)

    def start(self) -> None:
        self._thread.start()
        for row in self._db.query("SELECT title FROM movie"):
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
