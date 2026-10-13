"""Load the theater registry and showtimes into SQLite."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from moviexfone.db import Database
from moviexfone.locations import PlaceRef, TheaterRef
from moviexfone.registry_data import PLACES, THEATER_GEO
from moviexfone.sources.parse import (
    IncomingShow,
    movie_id_for,
    parse_alamo,
    parse_assemble,
    parse_nitehawk_html,
)
from moviexfone.timeutil import NY, business_date, iso_ny

log = logging.getLogger(__name__)

_CHAIN_ALIASES = ("amc", "regal", "alamo", "nitehawk")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def _status(raw: str) -> str:
    text = (raw or "").strip().lower()
    for name in ("open", "closed", "seasonal", "unverified"):
        if text.startswith(name):
            return name
    return "unverified"


def _screens(raw: str) -> int | None:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


# Fixture rows keep this stamp across reseeds. Live fetches pass the real fetch time.
FIXTURE_FETCHED_AT = datetime(2026, 10, 13, 12, 0, tzinfo=NY)


def seed(
    db: Database,
    data: Path,
    *,
    now: datetime,
    include_showtimes: bool = True,
) -> dict[str, int]:
    """Replace the registry. Fixture showtimes load only when include_showtimes is set.

    Live mode must pass include_showtimes=False so sample times are never served
    as real listings.
    """
    with db.transaction():
        db.execute("DELETE FROM showtime")
        db.execute("DELETE FROM movie_alias")
        db.execute("DELETE FROM movie")
        db.execute("DELETE FROM theater_alias")
        db.execute("DELETE FROM theater")
        theater_count = _load_theaters(db, data)
        movie_count = _load_movies(db, data)
        if include_showtimes:
            shows = _fixture_shows(data)
            _apply(db, shows["assemble"], mode="insert", now=FIXTURE_FETCHED_AT)
            _apply(db, shows["nitehawk"], mode="dates", now=FIXTURE_FETCHED_AT)
            _apply(db, shows["alamo"], mode="theaters", now=FIXTURE_FETCHED_AT)
        _mark_coverage(db, now)
        db.execute(
            "INSERT INTO ingest_run (source, started_at, finished_at, ok, theaters, showtimes, error) VALUES (?, ?, ?, 1, ?, ?, NULL)",
            (
                "fixtures" if include_showtimes else "registry",
                iso_ny(now),
                iso_ny(now),
                theater_count,
                _show_count(db),
            ),
        )
    return {"theaters": theater_count, "movies": movie_count, "showtimes": _show_count(db)}


def _load_theaters(db: Database, data: Path) -> int:
    path = data / "nyc-theaters.csv"
    rows: list[tuple[object, ...]] = []
    aliases: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            name = record["name"].strip()
            theater_id = _slug(name)
            geo = THEATER_GEO.get(name)
            lat = lng = None
            spoken = re.sub(r"\s*\(.*?\)", "", name).strip()
            website = None
            extra: tuple[str, ...] = ()
            if geo is not None:
                lat, lng, spoken, extra, website = geo
            operator = record.get("operator") or ""
            rows.append(
                (
                    theater_id,
                    name,
                    spoken,
                    operator,
                    record.get("tier"),
                    record.get("borough"),
                    record.get("neighborhood"),
                    record.get("address"),
                    None,
                    lat,
                    lng,
                    website,
                    record.get("ticketing_platform"),
                    _status(record.get("status") or ""),
                    _authoritative_source(name),
                    0,
                    _screens(record.get("screens") or ""),
                    None,
                    record.get("notes"),
                )
            )
            alias_set = {spoken, name, *_auto_aliases(name, operator), *extra}
            for alias in alias_set:
                cleaned = alias.strip().lower()
                if cleaned:
                    aliases.append((theater_id, cleaned))
    db.executemany(
        """INSERT INTO theater (
            theater_id, name, spoken_name, operator, tier, borough, neighborhood, address, zip,
            lat, lng, website, ticketing_platform, status, source, covered, screens,
            last_success_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    db.executemany("INSERT OR IGNORE INTO theater_alias (theater_id, alias) VALUES (?, ?)", aliases)
    return len(rows)


def _auto_aliases(name: str, operator: str) -> list[str]:
    found: list[str] = []
    blob = f"{name} {operator}".lower()
    for token in _CHAIN_ALIASES:
        if token in blob.split() or token in blob:
            # Only attach the chain token when the theater is that chain.
            if token in name.lower() or token in operator.lower():
                found.append(token)
    return found


def _authoritative_source(name: str) -> str:
    lower = name.lower()
    if lower.startswith("alamo"):
        return "alamo_json"
    if lower.startswith("nitehawk"):
        return "nitehawk_jsonld"
    return "assemble"


def _load_movies(db: Database, data: Path) -> int:
    payload = json.loads((data / "movies.json").read_text(encoding="utf-8"))
    movies: list[tuple[object, ...]] = []
    aliases: list[tuple[str, str]] = []
    for item in payload:
        movie_id = str(item["movie_id"])
        title = str(item["title"])
        movies.append(
            (
                movie_id,
                title,
                item.get("year"),
                item.get("rating"),
                item.get("runtime_min"),
                item.get("synopsis_short"),
                item.get("tmdb_id"),
            )
        )
        names = {title.lower(), *(str(alias).lower() for alias in item.get("aliases") or [])}
        for alias in names:
            aliases.append((movie_id, alias))
    db.executemany(
        """INSERT INTO movie (movie_id, title, year, rating, runtime_min, synopsis_short, tmdb_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        movies,
    )
    db.executemany("INSERT OR IGNORE INTO movie_alias (movie_id, alias) VALUES (?, ?)", aliases)
    return len(movies)


def _fixture_shows(data: Path) -> dict[str, list[IncomingShow]]:
    fixtures = data / "fixtures"
    alamo = parse_alamo(json.loads((fixtures / "alamo_nyc.json").read_text(encoding="utf-8")))
    assemble = parse_assemble(
        json.loads((fixtures / "assemble_screenings.json").read_text(encoding="utf-8"))
    )
    nitehawk: list[IncomingShow] = []
    nitehawk.extend(
        parse_nitehawk_html(
            (fixtures / "nitehawk_williamsburg.html").read_text(encoding="utf-8"),
            default_theater="Nitehawk Cinema Williamsburg",
        )
    )
    nitehawk.extend(
        parse_nitehawk_html(
            (fixtures / "nitehawk_prospectpark.html").read_text(encoding="utf-8"),
            default_theater="Nitehawk Cinema Prospect Park",
        )
    )
    return {"alamo": alamo, "assemble": assemble, "nitehawk": nitehawk}


def _apply(db: Database, rows: list[IncomingShow], *, mode: str, now: datetime) -> int:
    if not rows:
        return 0
    resolved = [_ResolvedShow.from_incoming(db, row) for row in rows]
    kept = [row for row in resolved if row is not None]
    if kept and kept[0].source == "assemble":
        kept = [row for row in kept if not _alamo_owns(db, row.theater_id)]
    if not kept:
        return 0
    # Replace only the movie-days in this payload. An Alamo page is authoritative
    # for those films. A short Nitehawk page must not wipe other films.
    groups = {
        (row.theater_id, row.movie_id, business_date(row.starts_at).isoformat()) for row in kept
    }
    for theater_id, movie_id, day in groups:
        db.execute(
            """DELETE FROM showtime
               WHERE theater_id = ? AND movie_id = ? AND business_date = ?""",
            (theater_id, movie_id, day),
        )
    fetched = iso_ny(now.astimezone(NY))
    payload = [
        (
            row.theater_id,
            row.movie_id,
            row.starts_at.astimezone(NY).isoformat(timespec="seconds"),
            row.starts_at.astimezone(NY).isoformat(timespec="seconds"),
            business_date(row.starts_at).isoformat(),
            row.format_norm,
            row.format_label,
            row.status,
            row.ticket_url,
            row.source,
            fetched,
        )
        for row in kept
    ]
    db.executemany(
        """INSERT OR REPLACE INTO showtime (
            theater_id, movie_id, starts_at, starts_local, business_date, format_norm,
            format_label, status, ticket_url, source, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    return len(payload)


def _delete_theaters(db: Database, theater_ids: list[str]) -> None:
    for theater_id in theater_ids:
        db.execute("DELETE FROM showtime WHERE theater_id = ?", (theater_id,))


def _delete_dates(db: Database, theater_id: str, dates: list[str]) -> None:
    for business in dates:
        db.execute(
            "DELETE FROM showtime WHERE theater_id = ? AND business_date = ?",
            (theater_id, business),
        )


class _ResolvedShow:
    def __init__(
        self,
        theater_id: str,
        movie_id: str,
        starts_at: datetime,
        format_norm: str,
        format_label: str | None,
        status: str,
        ticket_url: str | None,
        source: str,
    ) -> None:
        self.theater_id = theater_id
        self.movie_id = movie_id
        self.starts_at = starts_at
        self.format_norm = format_norm
        self.format_label = format_label
        self.status = status
        self.ticket_url = ticket_url
        self.source = source

    @staticmethod
    def from_incoming(db: Database, row: IncomingShow) -> _ResolvedShow | None:
        theater_id = _match_theater(db, row.theater_key)
        if theater_id is None:
            log.info("unmatched venue %s", row.theater_key)
            return None
        movie_id = _ensure_movie(db, row.movie_title)
        return _ResolvedShow(
            theater_id,
            movie_id,
            row.starts_at,
            row.format_norm,
            row.format_label,
            row.status,
            row.ticket_url,
            row.source,
        )


_GENERIC_VENUE = {"cinema", "cinemas", "theater", "theaters", "the", "and", "ua", "dine", "in"}
_VENUE_DROP = re.compile(r"\b(stadium|imax|rpx|4dx|dolby|screenx)\b", re.IGNORECASE)


def normalize_venue(name: str) -> str:
    text = name.lower().replace("&", " and ").replace("theatre", "theater")
    text = text.replace("cinemas", "cinema").replace("theaters", "theater")
    text = _VENUE_DROP.sub(" ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _venue_tokens(name: str) -> set[str]:
    return {token for token in normalize_venue(name).split() if token not in _GENERIC_VENUE}


def _match_venue_name(db: Database, key: str) -> str | None:
    wanted = _venue_tokens(key)
    if not wanted:
        return None
    best: list[tuple[int, str]] = []
    for theater in db.query(
        "SELECT theater_id, name, spoken_name FROM theater WHERE status = 'open'"
    ):
        aliases = [
            str(row["alias"])
            for row in db.query(
                "SELECT alias FROM theater_alias WHERE theater_id = ?", (theater["theater_id"],)
            )
        ]
        names = [str(theater["name"]), str(theater["spoken_name"] or ""), *aliases]
        overlap = 0
        for alias in names:
            tokens = _venue_tokens(alias)
            if not tokens:
                continue
            if wanted == tokens or wanted <= tokens or tokens <= wanted:
                overlap = max(overlap, len(wanted & tokens))
        if overlap:
            best.append((overlap, str(theater["theater_id"])))
    if not best:
        return None
    best.sort(reverse=True)
    if len(best) > 1 and best[0][0] == best[1][0] and best[0][1] != best[1][1]:
        return None
    return best[0][1]


def record_ingest(
    db: Database,
    *,
    source: str,
    now: datetime,
    ok: bool,
    showtimes: int,
    error: str | None,
) -> None:
    stamp = iso_ny(now)
    db.execute(
        """INSERT INTO ingest_run (source, started_at, finished_at, ok, theaters, showtimes, error)
           VALUES (?, ?, ?, ?, 0, ?, ?)""",
        (source, stamp, stamp, 1 if ok else 0, showtimes, error),
    )


def _alamo_owns(db: Database, theater_id: str) -> bool:
    row = db.query_one(
        "SELECT 1 AS present FROM showtime WHERE theater_id = ? AND source = 'alamo_json' LIMIT 1",
        (theater_id,),
    )
    return row is not None


def _match_theater(db: Database, key: str) -> str | None:
    direct = db.query_one("SELECT theater_id FROM theater WHERE theater_id = ?", (key,))
    if direct:
        return str(direct["theater_id"])
    lowered = key.strip().lower()
    alias = db.query_one("SELECT theater_id FROM theater_alias WHERE alias = ?", (lowered,))
    if alias:
        return str(alias["theater_id"])
    by_name = db.query_one(
        "SELECT theater_id FROM theater WHERE lower(name) = ? OR lower(spoken_name) = ?",
        (lowered, lowered),
    )
    if by_name:
        return str(by_name["theater_id"])
    fuzzy = _match_venue_name(db, key)
    if fuzzy:
        return fuzzy
    from moviexfone.locations import resolve_location

    theaters = load_theater_refs(db)
    hit = resolve_location(key, theaters, [])
    if hit.kind == "theater" and hit.theater_ids:
        return hit.theater_ids[0]
    return None


def _ensure_movie(db: Database, title: str) -> str:
    from moviexfone.titles import resolve_title

    existing = db.query_one("SELECT movie_id FROM movie_alias WHERE alias = ?", (title.lower(),))
    if existing:
        return str(existing["movie_id"])
    catalog = []
    for movie in db.query("SELECT movie_id, title FROM movie"):
        aliases = [
            str(row["alias"])
            for row in db.query(
                "SELECT alias FROM movie_alias WHERE movie_id = ?", (movie["movie_id"],)
            )
        ]
        catalog.append((str(movie["movie_id"]), str(movie["title"]), aliases))
    hit, _suggestions = resolve_title(title, catalog)
    if hit is not None and hit.score >= 0.9:
        db.execute(
            "INSERT OR IGNORE INTO movie_alias (movie_id, alias) VALUES (?, ?)",
            (hit.movie_id, title.lower()),
        )
        return hit.movie_id
    movie_id = movie_id_for(title)
    found = db.query_one("SELECT movie_id FROM movie WHERE movie_id = ?", (movie_id,))
    if not found:
        db.execute(
            "INSERT INTO movie (movie_id, title, year, rating, runtime_min, synopsis_short, tmdb_id) VALUES (?, ?, NULL, NULL, NULL, NULL, NULL)",
            (movie_id, title),
        )
    db.execute(
        "INSERT OR IGNORE INTO movie_alias (movie_id, alias) VALUES (?, ?)",
        (movie_id, title.lower()),
    )
    return movie_id


def _mark_coverage(db: Database, now: datetime) -> None:
    db.execute("UPDATE theater SET covered = 0")
    db.execute(
        """UPDATE theater SET covered = 1, last_success_at = ?
           WHERE theater_id IN (SELECT DISTINCT theater_id FROM showtime)""",
        (iso_ny(now),),
    )


def _show_count(db: Database) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM showtime")
    return int(row["n"]) if row else 0


def load_theater_refs(db: Database) -> list[TheaterRef]:
    refs: list[TheaterRef] = []
    for row in db.query("SELECT * FROM theater"):
        aliases = [
            str(item["alias"])
            for item in db.query(
                "SELECT alias FROM theater_alias WHERE theater_id = ?", (row["theater_id"],)
            )
        ]
        refs.append(
            TheaterRef(
                theater_id=str(row["theater_id"]),
                name=str(row["name"]),
                spoken_name=str(row["spoken_name"] or row["name"]),
                operator=str(row["operator"] or ""),
                borough=str(row["borough"] or ""),
                lat=row["lat"],
                lng=row["lng"],
                website=row["website"],
                status=str(row["status"]),
                covered=bool(row["covered"]),
                aliases=aliases,
            )
        )
    return refs


def load_places() -> list[PlaceRef]:
    return [
        PlaceRef(
            place_id=place_id,
            name=name,
            kind=kind,
            borough=borough,
            lat=lat,
            lng=lng,
            radius_mi=radius,
            aliases=list(aliases),
        )
        for place_id, name, kind, borough, lat, lng, radius, aliases in PLACES
    ]


# Re-export for type checkers that want the date helper used by tests.
def refresh_live(db: Database, *, now: datetime) -> dict[str, object]:
    """Pull first-party feeds. Failures are recorded and never raised."""
    from moviexfone.sources.fetch import fetch_alamo, fetch_nitehawk

    report: dict[str, object] = {}
    try:
        rows = fetch_alamo()
        with db.transaction():
            count = _apply(db, rows, mode="theaters", now=now)
            record_ingest(db, source="alamo_json", now=now, ok=True, showtimes=count, error=None)
        report["alamo"] = count
    except Exception as exc:
        log.warning("alamo refresh failed: %s", type(exc).__name__)
        report["alamo_error"] = type(exc).__name__
        record_ingest(
            db, source="alamo_json", now=now, ok=False, showtimes=0, error=type(exc).__name__
        )
    try:
        rows = fetch_nitehawk()
        with db.transaction():
            count = _apply(db, rows, mode="dates", now=now)
            record_ingest(
                db, source="nitehawk_jsonld", now=now, ok=True, showtimes=count, error=None
            )
        report["nitehawk"] = count
    except Exception as exc:
        log.warning("nitehawk refresh failed: %s", type(exc).__name__)
        report["nitehawk_error"] = type(exc).__name__
        record_ingest(
            db,
            source="nitehawk_jsonld",
            now=now,
            ok=False,
            showtimes=0,
            error=type(exc).__name__,
        )
    _mark_coverage(db, now)
    return report
