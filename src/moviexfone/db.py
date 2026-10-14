"""SQLite showtimes database. Queries are parameterized."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS theater (
  theater_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  spoken_name TEXT,
  operator TEXT,
  tier TEXT,
  borough TEXT,
  neighborhood TEXT,
  address TEXT,
  zip TEXT,
  lat REAL,
  lng REAL,
  website TEXT,
  ticketing_platform TEXT,
  status TEXT DEFAULT 'open',
  source TEXT,
  covered INTEGER DEFAULT 0,
  screens INTEGER,
  last_success_at TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS theater_alias (
  theater_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  PRIMARY KEY (theater_id, alias)
);
CREATE TABLE IF NOT EXISTS movie (
  movie_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  year INTEGER,
  rating TEXT,
  runtime_min INTEGER,
  synopsis_short TEXT,
  tmdb_id INTEGER
);
CREATE TABLE IF NOT EXISTS movie_alias (
  movie_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  PRIMARY KEY (movie_id, alias)
);
CREATE TABLE IF NOT EXISTS showtime (
  theater_id TEXT NOT NULL,
  movie_id TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  starts_local TEXT NOT NULL,
  business_date TEXT NOT NULL,
  format_norm TEXT DEFAULT 'unknown',
  format_label TEXT,
  status TEXT DEFAULT 'on_sale',
  ticket_url TEXT,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (theater_id, movie_id, starts_at, format_norm)
);
CREATE TABLE IF NOT EXISTS ingest_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,
  started_at TEXT,
  finished_at TEXT,
  ok INTEGER,
  theaters INTEGER,
  showtimes INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_showtime_date ON showtime (business_date);
CREATE INDEX IF NOT EXISTS idx_showtime_movie ON showtime (movie_id);
CREATE INDEX IF NOT EXISTS idx_showtime_theater ON showtime (theater_id);
CREATE TABLE IF NOT EXISTS assemble_status (
  movie_id TEXT PRIMARY KEY,
  checked_at TEXT
);
CREATE TABLE IF NOT EXISTS film_id (
  title_key TEXT PRIMARY KEY,
  tmdb_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS film_lookup_miss (
  title_key TEXT PRIMARY KEY,
  missed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_session_day (
  host TEXT NOT NULL,
  day TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  PRIMARY KEY (host, day)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._in_tx = False
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One immediate transaction. A reader cannot see a half-applied seed."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                yield
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._in_tx = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            if not self._in_tx:
                self._conn.commit()

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            if not self._in_tx:
                self._conn.commit()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
