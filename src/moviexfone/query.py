"""Showtime search. Location, time, and title work happen here, not in the model."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

from moviexfone.catalog import load_places, load_theater_refs
from moviexfone.db import Database
from moviexfone.geo import borough_of, haversine_miles
from moviexfone.locations import PlaceRef, Resolution, TheaterRef, resolve_location
from moviexfone.nlu import tonight_after
from moviexfone.sanitize import sanitize_display
from moviexfone.shaping import ShowCard, spoken_title, trim_cards
from moviexfone.timeutil import (
    NY,
    business_date,
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
    movie: str | None = None,
    dates: list[str],
    after_time: str | None = None,
    before_time: str | None = None,
    format_name: str = "any",
    page: int = 1,
    now: datetime,
    date_spoken: str,
    geocode: Any = None,
    date_unclear: bool = False,
) -> dict[str, Any]:
    if date_unclear:
        return {
            "error": "date_unclear",
            "speak": "Which day do you mean?",
            "note": "Ask which day. Do not guess today. Do not search again until they answer.",
        }
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
        has_rows = False
        if resolution.theater_ids:
            has_rows = (
                db.query_one(
                    "SELECT 1 AS n FROM showtime WHERE theater_id = ? LIMIT 1",
                    (resolution.theater_ids[0],),
                )
                is not None
            )
        if not has_rows:
            return _uncovered(resolution, date_spoken)

    movie_id, movie_title, movie_error = _movie(db, movie)
    if movie_error is not None:
        return movie_error

    after = parse_clock(after_time) if after_time else None
    before = parse_clock(before_time) if before_time else None
    after = tonight_after(now.astimezone(NY).time(), after, date_spoken)
    rows = _rows(db)

    def _matched(where: Resolution) -> list[_Row]:
        return [
            row
            for row in rows
            if row.business_date in dates
            and (movie_id is None or row.movie_id == movie_id)
            and _format_ok(row, format_name)
            and row.starts_at >= now
            and show_matches_after(row.local_t, after)
            and (before is None or row.local_t < before)
            and _in_resolution(row, where)
        ]

    matched = _matched(resolution)
    widened = False
    if (
        not matched
        and resolution.kind == "place"
        and (resolution.radius_mi is None or resolution.radius_mi < 2)
    ):
        resolution = replace(resolution, radius_mi=2.0)
        matched = _matched(resolution)
        widened = True
    if format_name not in {"", "any"} and not matched:
        return _format_gap(db, resolution, format_name, date_spoken, dates)

    if not matched:
        later = _later_tonight(rows, resolution, movie_id, date_spoken, now, after)
        if later is not None:
            return later
        if movie_id is None:
            return _nothing_listed(resolution, date_spoken, rows, now, dates)
        return _alternative(
            rows,
            resolution,
            movie_id,
            movie_title or "",
            dates,
            after,
            date_spoken,
            now,
            db,
        )

    cards = _cards(matched, resolution, broad=movie_id is None)
    when = _when_phrase(date_spoken, resolution, specific=movie_id is not None)
    max_cards = 2
    max_times = 6 if movie_id is not None else 2
    oldest = min((row.fetched_at for row in matched), default=now)
    caveat = staleness_phrase(oldest, now)
    if caveat and "current times" in caveat:
        payload = _payload(resolution, date_spoken, [], 0, caveat, oldest)
        payload["coverage_note"] = caveat
        payload["note"] = "Times are too old to read. Do not invent newer ones."
        return payload
    chosen, more, speak = trim_cards(
        cards,
        page=page,
        max_cards=max_cards,
        max_times=max_times,
        when=when,
        specific_movie=movie_id is not None,
        name_theater=resolution.kind == "theater",
        name_days=date_spoken == "this weekend",
        prefix=caveat or "",
    )
    used = [
        row
        for row in matched
        if any(
            row.theater_id == card.theater_id and row.movie_id == card.movie_id for card in chosen
        )
    ]
    if used:
        oldest = min(row.fetched_at for row in used)
    first_party = chosen and all(
        card.source in {"alamo_json", "nitehawk_jsonld"} for card in chosen
    )
    if movie_id is None and chosen and not first_party:
        started = db.query_one("SELECT COUNT(*) AS n FROM assemble_status")
        pending = db.query_one(
            """SELECT COUNT(*) AS n FROM movie m
               WHERE NOT EXISTS (
                   SELECT 1 FROM assemble_status s WHERE s.movie_id = m.movie_id
               )"""
        )
        if started and int(started["n"]) > 0 and pending and int(pending["n"]) > 0:
            speak = f"{speak} I only have live times for some titles.".strip()
    if widened and chosen:
        speak = _mark_widened(speak, resolution.label, chosen)
    payload = _payload(resolution, date_spoken, chosen, more, speak, oldest)
    if not chosen:
        payload["note"] = "No further listings. Do not search again."
        payload["speak"] = f"I don't have more live times for {resolution.label}."
        payload["coverage_note"] = payload["speak"]
    return payload


def get_movie_info(db: Database, movie: str) -> dict[str, Any]:
    movie_id, title, error = _movie(db, movie)
    if error is not None:
        return error
    row = db.query_one("SELECT * FROM movie WHERE movie_id = ?", (movie_id,))
    if row is None or title is None:
        return {"error": "movie_not_found", "did_you_mean": [], "speak": "I don't have that title."}
    title = sanitize_display(title, limit=120)
    if not title:
        return {"error": "movie_not_found", "did_you_mean": [], "speak": "I don't have that title."}
    runtime = row["runtime_min"]
    rating = sanitize_display(row["rating"], limit=12)
    synopsis = sanitize_display(row["synopsis_short"], limit=160)
    bits: list[str] = []
    if rating:
        bits.append(rating)
    if runtime:
        bits.append(f"{runtime} minutes")
    if bits and synopsis:
        speak = f"{title} is {', '.join(bits)}. {synopsis}"
    elif synopsis:
        speak = f"{title}. {synopsis}"
    elif bits:
        speak = f"{title} is {', '.join(bits)}."
    else:
        speak = f"I don't have a synopsis or runtime for {title}."
    return {
        "movie": title,
        "rating": rating or None,
        "runtime_min": runtime,
        "synopsis": synopsis or None,
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
        names = [spoken_title(item.title) for item in suggestions]
        if len(names) >= 2:
            if len(names) == 2:
                speak = f"Do you mean {names[0]} or {names[1]}?"
            else:
                speak = "Do you mean " + ", ".join(names[:-1]) + f", or {names[-1]}?"
        elif names:
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
        if not first.theater or not first.movie:
            continue
        cards.append(
            ShowCard(
                theater=first.theater,
                theater_id=first.theater_id,
                movie=first.movie,
                movie_id=first.movie_id,
                distance_mi=_distance(first, resolution),
                times_local=[item.local_t for item in bucket],
                day_labels=[item.starts_at.astimezone(NY).strftime("%A") for item in bucket],
                format_norm=first.format_norm,
                ticket_url=first.ticket_url,
                source=first.source,
                borough=first.borough,
            )
        )
    theater_ids = {card.theater_id for card in cards}
    if broad and resolution.kind != "theater" and len(theater_ids) > 1:
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
    label = sanitize_display(resolution.label, limit=80) or "that area"
    if specific or resolution.kind == "theater":
        return when
    if resolution.kind == "borough":
        return f"{when} in {label}"
    if resolution.kind == "chain":
        return f"{when} at {label}"
    return f"{when} near {label}"


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


def _nothing_listed(
    resolution: Resolution,
    date_spoken: str,
    rows: list[_Row],
    now: datetime,
    dates: list[str],
) -> dict[str, Any]:
    speak = f"I don't have live times I can see for {resolution.label}."
    upcoming = [row for row in rows if row.starts_at >= now and row.business_date in dates]
    alternative = None
    if upcoming and resolution.lat is not None and resolution.lng is not None:
        upcoming.sort(key=lambda row: _distance(row, resolution) or 99.0)
        nearest = upcoming[0]
        miles = _distance(nearest, resolution)
        if miles is not None and miles <= 15:
            alternative = {
                "theater": nearest.theater,
                "distance_mi": miles,
                "movie": nearest.movie,
                "times": [display_time(nearest.local_t)],
                "format": None,
                "ticket_url": nearest.ticket_url,
            }
            day = nearest.starts_at.astimezone(NY).strftime("%A")
            speak = _clock_sentence(
                f"{speak} The closest listing I have is {nearest.movie} at {nearest.theater} "
                f"on {day} at",
                spoken_time(nearest.local_t),
            )
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "nearest_alternative": alternative,
        "speak": speak,
        "coverage_note": f"I don't have live times I can see for {resolution.label}.",
        "note": "Say that no live times are listed. Offer nearest_alternative if present. Do not search again.",
        "source": None,
        "data_as_of": iso_ny(now),
    }


def _alternative(
    rows: list[_Row],
    resolution: Resolution,
    movie_id: str,
    movie_title: str,
    dates: list[str],
    after: time | None,
    date_spoken: str,
    now: datetime,
    db: Database,
) -> dict[str, Any]:
    same_place = [
        row
        for row in rows
        if row.movie_id == movie_id
        and row.business_date in dates
        and row.starts_at >= now
        and _in_resolution(row, resolution)
    ]
    pool = same_place
    elsewhere = False
    if not pool:
        pool = [
            row
            for row in rows
            if row.movie_id == movie_id and row.business_date in dates and row.starts_at >= now
        ]
        elsewhere = True
    if not pool:
        other_day = _other_day(rows, resolution, movie_id, movie_title, date_spoken, now)
        if other_day is not None:
            return other_day
        if _title_not_fetched(db, movie_id):
            speak = f"I don't have times for {spoken_title(movie_title)} at {resolution.label} yet."
        else:
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
        if _title_not_fetched(db, movie_id):
            speak = (
                f"I don't have times for {spoken_title(movie_title)} at {where} yet. "
                f"The closest I already have is {clock} at {only.theater}."
            )
        else:
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


def _other_day(
    rows: list[_Row],
    resolution: Resolution,
    movie_id: str,
    movie_title: str,
    date_spoken: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Nearest other date for this title at this theater, when the asked day is empty."""
    upcoming = [
        row
        for row in rows
        if row.movie_id == movie_id and row.starts_at >= now and _in_resolution(row, resolution)
    ]
    if not upcoming:
        return None
    first = min(upcoming, key=lambda row: (row.starts_at, row.local_t))
    today = business_date(now).isoformat()
    tomorrow = (business_date(now) + timedelta(days=1)).isoformat()
    if first.business_date == today:
        when = "today"
    elif first.business_date == tomorrow:
        when = "tomorrow"
    else:
        when = first.starts_at.astimezone(NY).strftime("%A")
    title = spoken_title(movie_title)
    same_day = sorted(
        (
            row
            for row in upcoming
            if row.theater_id == first.theater_id and row.business_date == first.business_date
        ),
        key=lambda row: row.local_t,
    )[:3]
    clocks = [spoken_time(row.local_t) for row in same_day]
    if len(clocks) == 1:
        listed = clocks[0]
    elif len(clocks) == 2:
        listed = f"{clocks[0]} and {clocks[1]}"
    else:
        listed = f"{clocks[0]}, {clocks[1]}, and {clocks[2]}"
    speak = f"{title} plays {when} at {listed} at {first.theater}."
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "nearest_alternative": {
            "theater": first.theater,
            "movie": title,
            "times": [display_time(row.local_t) for row in same_day],
            "ticket_url": first.ticket_url,
        },
        "speak": speak,
        "note": "That day is empty here. The nearest date at this theater is in speak. Do not say the times are not loaded.",
        "coverage_note": None,
        "source": first.source,
        "data_as_of": iso_ny(now),
    }


def _farther_words(miles: float) -> str:
    if miles <= 1.2:
        return "about a mile away"
    if miles <= 1.7:
        return "about a mile and a half away"
    return "about two miles away"


def _where_words(row: _Row, resolution: Resolution) -> str:
    miles = _distance(row, resolution)
    if miles is not None and miles > 1:
        return f"at {row.theater}, {_farther_words(miles)}"
    return f"at {row.theater}"


def _with_farther(speak: str, row: _Row, resolution: Resolution) -> str:
    miles = _distance(row, resolution)
    if miles is None or miles <= 1:
        return speak
    extra = _farther_words(miles)
    if speak.endswith("."):
        return speak[:-1] + f", {extra}."
    return f"{speak}, {extra}."


def _mark_widened(speak: str, label: str, cards: list[ShowCard]) -> str:
    miles = [
        card.distance_mi for card in cards if card.distance_mi is not None and card.distance_mi > 1
    ]
    if not miles:
        return speak
    away = _farther_words(min(miles))
    needle = f" near {label}"
    if needle in speak:
        return speak.replace(needle, f", a bit farther from {label}, {away}", 1)
    if speak.endswith("."):
        return speak[:-1] + f", a bit farther, {away}."
    return f"{speak}, a bit farther, {away}."


def _clock_sentence(prefix: str, clock: str) -> str:
    text = f"{prefix} {clock}".rstrip()
    if text.endswith("."):
        return text
    return f"{text}."


def _title_not_fetched(db: Database, movie_id: str) -> bool:
    """True until Assemble has finished a pass for this title.

    Rows from Alamo or Nitehawk do not count. Those feeds do not cover AMC or Regal,
    and saying "no screenings" during warm-up is a false answer.
    """
    checked = db.query_one(
        "SELECT 1 AS n FROM assemble_status WHERE movie_id = ?",
        (movie_id,),
    )
    return checked is None


def _later_tonight(
    rows: list[_Row],
    resolution: Resolution,
    movie_id: str | None,
    date_spoken: str,
    now: datetime,
    after: time | None = None,
) -> dict[str, Any] | None:
    if resolution.kind == "theater" and not resolution.covered:
        return None
    if resolution.kind not in {"theater", "place"}:
        return None
    spoken = date_spoken.lower()
    named_tonight = "tonight" in spoken
    local = now.astimezone(NY).time()
    # No date and no useful evening floor: 10 PM through the 3 AM cinema rollover.
    late_clock = local >= time(22, 0) or local < time(3, 0)
    evening_today = spoken == "today" and (
        (after is not None and after >= time(17, 0)) or late_clock
    )
    if not named_tonight and not evening_today:
        return None
    tomorrow = (business_date(now) + timedelta(days=1)).isoformat()
    upcoming = [
        row
        for row in rows
        if row.business_date == tomorrow
        and row.starts_at >= now
        and (movie_id is None or row.movie_id == movie_id)
        and _in_resolution(row, resolution)
    ]
    if not upcoming:
        return None
    picks: list[_Row] = []
    seen: set[str] = set()
    for row in sorted(upcoming, key=lambda item: item.starts_at):
        if movie_id is None and row.theater_id in seen:
            continue
        seen.add(row.theater_id)
        picks.append(row)
        if len(picks) == (1 if movie_id is not None else 3):
            break
    if len(picks) == 1:
        first = picks[0]
        speak = _clock_sentence(
            f"Nothing else tonight. Tomorrow the first show is {spoken_title(first.movie)} at",
            spoken_time(first.local_t),
        )
        speak = _with_farther(speak, first, resolution)
    else:
        bits = [
            f"{spoken_title(row.movie)} at {spoken_time(row.local_t)} {_where_words(row, resolution)}"
            for row in picks
        ]
        if len(bits) == 2:
            listed = f"{bits[0]} and {bits[1]}"
        else:
            listed = ", ".join(bits[:-1]) + f", and {bits[-1]}"
        farther = any((_distance(row, resolution) or 0) > 1 for row in picks)
        lead = "first shows are" if farther else "first shows nearby are"
        speak = f"Nothing else tonight. Tomorrow the {lead} {listed}."
        first = picks[0]
    return {
        "resolved_location": _resolved_json(resolution),
        "date_spoken": date_spoken,
        "results": [],
        "more_results": 0,
        "speak": speak,
        "coverage_note": speak,
        "nearest_alternative": None,
        "note": "The theater is covered. Tonight is over. Do not say there are no live times.",
        "source": first.source,
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
