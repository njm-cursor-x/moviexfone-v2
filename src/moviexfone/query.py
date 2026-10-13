"""Showtime search. Location, time, and title work happen here, not in the model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from moviexfone.catalog import load_places, load_theater_refs
from moviexfone.db import Database
from moviexfone.geo import borough_of, haversine_miles
from moviexfone.locations import PlaceRef, Resolution, TheaterRef, resolve_location
from moviexfone.nlu import tonight_after
from moviexfone.sanitize import sanitize_display
from moviexfone.shaping import ShowCard, trim_cards
from moviexfone.timeutil import (
    NY,
    display_time,
    iso_ny,
    parse_clock,
    show_matches_after,
    spoken_time,
)
from moviexfone.titles import resolve_title

_SPECIAL_FORMATS = {"imax", "dolby", "3d", "70mm", "35mm", "4dx", "rpx"}


@dataclass
class _Row:
    theater_id: str
    theater: str
    borough: str
    lat: float | None
    lng: float | None
    movie_id: str
    movie: str
    starts_at: datetime
    local_t: time
    business_date: str
    format_norm: str
    ticket_url: str | None
    source: str
    fetched_at: datetime
    covered: bool
    website: str | None


def search_showtimes(
    db: Database,
    *,
    location: str,
    movie: str | None,
    dates: list[str],
    after_time: str | None,
    format_name: str,
    page: int,
    now: datetime,
    date_spoken: str,
    geocode: Any = None,
) -> dict[str, Any]:
    theaters = _theaters(db)
    places = _places()
    resolution = resolve_location(location, theaters, places, geocode=geocode)
    if resolution.kind == "ambiguous":
        options = resolution.options
        speak = f"Do you mean {' or '.join(options[:2])}?"
        if len(options) > 2:
            speak = "Which one: " + ", ".join(options[:3]) + "?"
        return {
            "error": "location_ambiguous",
            "options": options,
            "speak": speak,
            "note": "Ask one short question using the options. Do not search again until they answer.",
        }
    if resolution.kind == "not_found":
        return {
            "error": "location_not_found",
            "ask": "Which neighborhood or theater?",
            "speak": "Which neighborhood or theater?",
        }
    if resolution.kind == "theater" and resolution.covered is False:
        return _uncovered(resolution, date_spoken)

    movie_id, movie_title, movie_error = _movie(db, movie)
    if movie_error is not None:
        return movie_error

    after = parse_clock(after_time) if after_time else None
    after = tonight_after(now.astimezone(NY).time(), after, date_spoken)
    rows = _rows(db)
    matched = [
        row
        for row in rows
        if row.business_date in dates
        and (movie_id is None or row.movie_id == movie_id)
        and _format_ok(row, format_name)
        and show_matches_after(row.local_t, after)
        and _in_resolution(row, resolution)
    ]
    if format_name not in {"", "any"} and not matched:
        return _format_gap(db, resolution, format_name, date_spoken, dates)

    if not matched and movie_id is not None:
        return _alternative(
            rows,
            resolution,
            movie_id,
            movie_title or "",
            dates,
            after,
            date_spoken,
            now,
        )

    cards = _cards(matched, resolution, broad=movie_id is None)
    when = _when_phrase(date_spoken, resolution, specific=movie_id is not None)
    max_cards = 2
    max_times = 3 if movie_id is not None else 2
    chosen, more, speak = trim_cards(
        cards,
        page=page,
        max_cards=max_cards,
        max_times=max_times,
        when=when,
        specific_movie=movie_id is not None,
    )
    used = [
        row
        for row in matched
        if any(
            row.theater_id == card.theater_id and row.movie_id == card.movie_id for card in chosen
        )
    ]
    oldest = min((row.fetched_at for row in used), default=now)
    caveat = staleness_phrase(oldest, now)
    if caveat and "current times" in caveat:
        speak = caveat
    elif caveat:
        speak = f"{caveat} {speak}".strip()
    payload = _payload(resolution, date_spoken, chosen, more, speak, oldest)
    if not chosen:
        payload["note"] = "No further listings. Do not search again."
        payload["speak"] = "That's everything I have on that."
    return payload


def get_movie_info(db: Database, movie: str) -> dict[str, Any]:
    movie_id, title, error = _movie(db, movie)
    if error is not None:
        return error
    row = db.query_one("SELECT * FROM movie WHERE movie_id = ?", (movie_id,))
    if row is None or title is None:
        return {"error": "movie_not_found", "did_you_mean": [], "speak": "I don't have that title."}
    runtime = row["runtime_min"]
    rating = sanitize_display(row["rating"], limit=12) or "unrated"
    synopsis = (
        sanitize_display(row["synopsis_short"], limit=160)
        or "I don't have a synopsis for that one."
    )
    spoken_runtime = f"{runtime} minutes" if runtime else "an unknown runtime"
    speak = f"{title} is {rating}, {spoken_runtime}. {synopsis}"
    return {
        "movie": title,
        "rating": rating or None,
        "runtime_min": runtime,
        "synopsis": synopsis,
        "speak": speak,
    }


def end_call() -> dict[str, Any]:
    return {"ok": True, "action": "end_call", "speak": "Enjoy the show."}


def _uncovered(resolution: Resolution, date_spoken: str) -> dict[str, Any]:
    name = resolution.label
    note = f"I don't have live times for {name}. Their website has the schedule."
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "speak": note,
        "coverage_note": note,
        "website": resolution.website,
        "nearest_alternative": None,
        "note": "Say the coverage note. Do not invent times. Do not search again.",
        "source": None,
        "data_as_of": None,
    }


def _movie(db: Database, movie: str | None) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if not movie or not movie.strip():
        return None, None, None
    catalog = _catalog(db)
    hit, suggestions = resolve_title(movie, catalog)
    if hit is None:
        names = [item.title for item in suggestions]
        if names:
            speak = f"Do you mean {names[0]}?"
        else:
            speak = "I don't have that title. Try another one?"
        return (
            None,
            None,
            {
                "error": "movie_not_found",
                "did_you_mean": names,
                "speak": speak,
                "note": "Ask one short question. Do not invent a title.",
            },
        )
    return hit.movie_id, hit.title, None


def _catalog(db: Database) -> list[tuple[str, str, list[str]]]:
    catalog: list[tuple[str, str, list[str]]] = []
    for movie in db.query("SELECT movie_id, title FROM movie"):
        aliases = [
            str(row["alias"])
            for row in db.query(
                "SELECT alias FROM movie_alias WHERE movie_id = ?", (movie["movie_id"],)
            )
        ]
        catalog.append((str(movie["movie_id"]), str(movie["title"]), aliases))
    return catalog


def _theaters(db: Database) -> list[TheaterRef]:
    refs = load_theater_refs(db)
    return [ref for ref in refs if isinstance(ref, TheaterRef)]


def _places() -> list[PlaceRef]:
    return [place for place in load_places() if isinstance(place, PlaceRef)]


def _rows(db: Database) -> list[_Row]:
    sql = """
    SELECT s.*, t.spoken_name, t.borough, t.lat, t.lng, t.covered, t.website, t.status,
           m.title AS movie_title
    FROM showtime s
    JOIN theater t ON t.theater_id = s.theater_id
    JOIN movie m ON m.movie_id = s.movie_id
    WHERE t.status = 'open' AND s.status = 'on_sale'
    """
    parsed: list[_Row] = []
    for row in db.query(sql):
        starts = datetime.fromisoformat(str(row["starts_at"]))
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=NY)
        fetched = datetime.fromisoformat(str(row["fetched_at"]))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=NY)
        parsed.append(
            _Row(
                theater_id=str(row["theater_id"]),
                theater=sanitize_display(str(row["spoken_name"]), limit=80),
                borough=str(row["borough"] or ""),
                lat=row["lat"],
                lng=row["lng"],
                movie_id=str(row["movie_id"]),
                movie=sanitize_display(str(row["movie_title"]), limit=80),
                starts_at=starts,
                local_t=starts.astimezone(NY).time().replace(second=0, microsecond=0),
                business_date=str(row["business_date"]),
                format_norm=str(row["format_norm"] or "unknown"),
                ticket_url=row["ticket_url"],
                source=str(row["source"]),
                fetched_at=fetched,
                covered=bool(row["covered"]),
                website=row["website"],
            )
        )
    return parsed


def _format_ok(row: _Row, format_name: str) -> bool:
    if format_name in {"", "any"}:
        return True
    return row.format_norm == format_name


def _in_resolution(row: _Row, resolution: Resolution) -> bool:
    if resolution.kind in {"theater", "chain"}:
        return row.theater_id in resolution.theater_ids
    if resolution.kind == "borough":
        if row.lat is None or row.lng is None:
            return row.borough == resolution.borough
        return borough_of(row.lat, row.lng) == resolution.borough
    if resolution.lat is None or resolution.lng is None or row.lat is None or row.lng is None:
        return False
    radius = resolution.radius_mi if resolution.radius_mi is not None else 1.0
    return haversine_miles(resolution.lat, resolution.lng, row.lat, row.lng) <= radius


def _distance(row: _Row, resolution: Resolution) -> float | None:
    if resolution.lat is None or resolution.lng is None or row.lat is None or row.lng is None:
        return None
    return round(haversine_miles(resolution.lat, resolution.lng, row.lat, row.lng), 1)


def _cards(rows: list[_Row], resolution: Resolution, *, broad: bool) -> list[ShowCard]:
    grouped: dict[tuple[str, str, str], list[_Row]] = {}
    for row in rows:
        grouped.setdefault((row.theater_id, row.movie_id, row.format_norm), []).append(row)
    cards: list[ShowCard] = []
    for bucket in grouped.values():
        bucket.sort(key=lambda item: item.starts_at)
        first = bucket[0]
        cards.append(
            ShowCard(
                theater=first.theater,
                theater_id=first.theater_id,
                movie=first.movie,
                movie_id=first.movie_id,
                distance_mi=_distance(first, resolution),
                times_local=[item.local_t for item in bucket],
                format_norm=first.format_norm,
                ticket_url=first.ticket_url,
                source=first.source,
                borough=first.borough,
            )
        )
    if broad and resolution.kind != "theater":
        per_theater: dict[str, ShowCard] = {}
        for card in sorted(
            cards, key=lambda item: item.times_local[0] if item.times_local else time(23, 59)
        ):
            current = per_theater.get(card.theater_id)
            if current is None or card.times_local[0] < current.times_local[0]:
                per_theater[card.theater_id] = card
        cards = list(per_theater.values())
    cards.sort(
        key=lambda card: (
            card.distance_mi is None,
            card.distance_mi or 0,
            card.times_local[0],
            card.theater,
        )
    )
    return cards


def _when_phrase(date_spoken: str, resolution: Resolution, *, specific: bool) -> str:
    when = date_spoken[:1].upper() + date_spoken[1:]
    if specific:
        return when
    if resolution.kind == "theater":
        return f"{when} at {resolution.label}"
    if resolution.kind == "borough":
        return f"{when} in {resolution.label}"
    if resolution.kind == "chain":
        return f"{when} at {resolution.label}"
    return f"{when} near {resolution.label}"


def _payload(
    resolution: Resolution,
    date_spoken: str,
    cards: list[ShowCard],
    more: int,
    speak: str,
    now: datetime,
) -> dict[str, Any]:
    fetched = [card for card in cards]
    sources = {card.source for card in fetched}
    source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None)
    results = []
    for card in cards:
        fmt = card.format_norm if card.format_norm in _SPECIAL_FORMATS else None
        results.append(
            {
                "theater": card.theater,
                "theater_id": card.theater_id,
                "borough": card.borough,
                "distance_mi": card.distance_mi,
                "movie": card.movie,
                "times": [display_time(moment) for moment in card.times_local],
                "format": fmt,
                "ticket_url": card.ticket_url,
                "source": card.source,
            }
        )
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": results,
        "more_results": more,
        "speak": speak,
        "data_as_of": iso_ny(now),
        "source": source,
        "coverage_note": None,
        "nearest_alternative": None,
        "note": None,
    }


def _resolved_json(resolution: Resolution) -> dict[str, Any]:
    return {
        "label": resolution.label,
        "kind": resolution.kind,
        "borough": resolution.borough,
    }


def _format_gap(
    db: Database,
    resolution: Resolution,
    format_name: str,
    date_spoken: str,
    dates: list[str],
) -> dict[str, Any]:
    del dates
    any_known = db.query_one(
        "SELECT 1 AS n FROM showtime WHERE format_norm = ? LIMIT 1", (format_name,)
    )
    if any_known is None:
        speak = (
            f"I don't have {_format_words(format_name)} listings. "
            "I only get format labels from Alamo, and none of those match."
        )
    else:
        speak = f"No {_format_words(format_name)} listings for that."
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "speak": speak,
        "coverage_note": speak,
        "nearest_alternative": None,
        "note": "Do not invent a format or a time. Do not search again.",
        "source": "alamo_json",
        "data_as_of": None,
    }


def _format_words(fmt: str) -> str:
    return {
        "imax": "IMAX",
        "dolby": "Dolby",
        "35mm": "35 millimeter",
        "70mm": "70 millimeter",
        "3d": "3D",
    }.get(fmt, fmt)


def _alternative(
    rows: list[_Row],
    resolution: Resolution,
    movie_id: str,
    movie_title: str,
    dates: list[str],
    after: time | None,
    date_spoken: str,
    now: datetime,
) -> dict[str, Any]:
    same_place = [
        row
        for row in rows
        if row.movie_id == movie_id
        and row.business_date in dates
        and _in_resolution(row, resolution)
    ]
    pool = same_place
    elsewhere = False
    if not pool:
        pool = [row for row in rows if row.movie_id == movie_id and row.business_date in dates]
        elsewhere = True
    if not pool:
        speak = f"No screenings listed for {movie_title}. Want another movie or neighborhood?"
        return {
            "resolved_location": _resolved_json(resolution),
            "date_spoken": date_spoken,
            "results": [],
            "more_results": 0,
            "nearest_alternative": None,
            "speak": speak,
            "note": "No screenings listed. Offer another movie or neighborhood. Do not search again.",
            "coverage_note": None,
            "source": None,
            "data_as_of": iso_ny(now),
        }
    if elsewhere:

        def sort_distance(row: _Row) -> float:
            miles = _distance(row, resolution)
            return 99.0 if miles is None else miles

        pool.sort(key=sort_distance)
        best_theater = pool[0].theater_id
        chosen_rows = [row for row in pool if row.theater_id == best_theater]
    else:
        target = after or time(17, 0)
        chosen_rows = sorted(pool, key=lambda row: _time_gap(row.local_t, target))[:1]
    card_rows = chosen_rows[:3]
    card = _cards(card_rows, resolution, broad=False)
    if not card:
        speak = f"No screenings listed for {movie_title}."
        alternative = None
    else:
        only = card[0]
        only = ShowCard(
            theater=only.theater,
            theater_id=only.theater_id,
            movie=only.movie,
            movie_id=only.movie_id,
            distance_mi=only.distance_mi,
            times_local=only.times_local[:1] if not elsewhere else only.times_local[:3],
            format_norm=only.format_norm,
            ticket_url=only.ticket_url,
            source=only.source,
            borough=only.borough,
        )
        clock = spoken_time(only.times_local[0])
        where = resolution.label
        speak = (
            f"No screenings listed for {movie_title} at {where}"
            + (f" after {_after_words(after)}" if after and not elsewhere else "")
            + f". The closest is {clock} at {only.theater}."
        )
        fmt = only.format_norm if only.format_norm in _SPECIAL_FORMATS else None
        alternative = {
            "theater": only.theater,
            "distance_mi": only.distance_mi,
            "movie": only.movie,
            "times": [display_time(moment) for moment in only.times_local],
            "format": fmt,
            "ticket_url": only.ticket_url,
        }
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "nearest_alternative": alternative,
        "speak": speak,
        "note": "No screenings listed. Offer nearest_alternative if present. Do not search again.",
        "coverage_note": None,
        "source": card[0].source if card else None,
        "data_as_of": iso_ny(now),
    }


def _time_gap(local_t: time, target: time) -> int:
    local_m = local_t.hour * 60 + local_t.minute
    target_m = target.hour * 60 + target.minute
    if local_t < time(3, 0):
        local_m += 24 * 60
    return abs(local_m - target_m)


def _after_words(after: time | None) -> str:
    if after is None:
        return ""
    return spoken_time(after)


def staleness_phrase(fetched_at: datetime, now: datetime) -> str | None:
    age = now - fetched_at
    if age > timedelta(hours=24):
        return "I don't have current times for that."
    if age > timedelta(hours=6):
        return "These times are from earlier today."
    return None
