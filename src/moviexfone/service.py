"""Answer a caller sentence with the tools layer. Used by the demo page and the eval."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from moviexfone.db import Database
from moviexfone.nlu import parse_question
from moviexfone.query import end_call, get_movie_info, search_showtimes


def ask(
    db: Database, text: str, *, now: datetime, page: int = 1, geocode: Any = None
) -> dict[str, Any]:
    titles = _titles(db)
    parsed = parse_question(text, now=now, titles=titles)
    if parsed.intent == "end_call":
        result = end_call()
    elif parsed.intent == "movie_info":
        result = get_movie_info(db, parsed.movie or text)
    elif not parsed.location:
        result = {
            "error": "need_location",
            "ask": "Which neighborhood or theater?",
            "speak": "Which neighborhood or theater?",
        }
    else:
        result = search_showtimes(
            db,
            location=parsed.location,
            movie=parsed.movie,
            dates=list(parsed.dates),
            after_time=parsed.after_time,
            format_name=parsed.format,
            page=page,
            now=now,
            date_spoken=parsed.date_spoken,
            geocode=geocode,
            date_unclear=parsed.date_unclear,
        )
    result = dict(result)
    result["parsed"] = {
        "intent": parsed.intent,
        "location": parsed.location,
        "movie": parsed.movie,
        "dates": list(parsed.dates),
        "after_time": parsed.after_time,
        "format": parsed.format,
        "date_spoken": parsed.date_spoken,
    }
    return result


def _titles(db: Database) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for movie in db.query("SELECT movie_id, title FROM movie"):
        aliases = [
            str(row["alias"])
            for row in db.query(
                "SELECT alias FROM movie_alias WHERE movie_id = ?", (movie["movie_id"],)
            )
        ]
        aliases.append(str(movie["title"]))
        found.append((str(movie["title"]), aliases))
    return found
