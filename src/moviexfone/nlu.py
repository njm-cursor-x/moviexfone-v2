"""Deterministic reading of a caller's sentence. The live model fills tool args itself."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

from moviexfone.timeutil import (
    NY,
    business_date,
    parse_after_phrase,
    resolve_when,
)

_FILLER = {
    "what",
    "whats",
    "whens",
    "wheres",
    "heres",
    "theres",
    "is",
    "are",
    "playing",
    "when",
    "where",
    "can",
    "i",
    "see",
    "the",
    "a",
    "an",
    "at",
    "near",
    "around",
    "in",
    "on",
    "tonight",
    "today",
    "tomorrow",
    "times",
    "time",
    "show",
    "showing",
    "showtimes",
    "anywhere",
    "anything",
    "about",
    "how",
    "long",
    "and",
    "for",
    "me",
    "please",
    "movie",
    "movies",
    "this",
    "weekend",
    "after",
    "later",
    "just",
    "ones",
    "them",
    "it",
    "tell",
    "give",
    "get",
    "there",
    "some",
    "something",
    "night",
    "of",
    "to",
    "do",
    "you",
    "have",
    "got",
    "any",
    "im",
    "its",
    "with",
    "from",
    "that",
    "thats",
    "all",
    "thanks",
    "thank",
    "bye",
    "goodbye",
}
_WEEKDAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_FORMATS = (
    (r"\bimax\b", "imax"),
    (r"\bdolby\b", "dolby"),
    (r"\b70\s*(?:mm|millimeter)\b", "70mm"),
    (r"\b35\s*(?:mm|millimeter)\b", "35mm"),
    (r"\b3d\b", "3d"),
)


@dataclass(frozen=True)
class ParsedQuestion:
    intent: str
    location: str | None
    movie: str | None
    date: str | None
    dates: tuple[str, ...]
    after_time: str | None
    format: str
    date_spoken: str
    date_unclear: bool = False
    before_time: str | None = None


def parse_question(
    text: str,
    *,
    now: datetime,
    titles: list[tuple[str, list[str]]],
) -> ParsedQuestion:
    """`titles` is (canonical title, aliases including the title)."""
    raw = _flatten(text)
    intent = _intent(raw)
    fmt, raw_left = _format(raw)
    after = parse_after_phrase(raw)
    dates, spoken, raw_left, unclear = _dates(raw_left, now)
    window = resolve_when(spoken, now)
    if after is None and window.after is not None:
        after = window.after
    before = window.before
    movie, raw_left = _movie(raw_left, titles)
    if after is not None:
        raw_left = re.sub(
            r"\bafter\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
            r"(?:\s*:\s*\d{2}|\s+(?:oh five|o five|fifteen|thirty|forty-five|forty five))?"
            r"\s*(?:am|pm)?\b",
            " ",
            raw_left,
        )
    location = _location(raw_left) if intent == "search" else None
    if intent == "movie_info" and movie is None:
        movie, _ = _movie(_flatten(text), titles)
    return ParsedQuestion(
        intent=intent,
        location=location,
        movie=movie,
        date=dates[0] if len(dates) == 1 else None,
        dates=tuple(dates),
        after_time=after.strftime("%H:%M") if after else None,
        before_time=before.strftime("%H:%M") if before else None,
        format=fmt,
        date_spoken=spoken,
        date_unclear=unclear,
    )


def _flatten(text: str) -> str:
    lowered = text.lower().replace("'", "")
    lowered = re.sub(r"[^a-z0-9:\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _intent(text: str) -> str:
    if re.search(r"\b(goodbye|bye|hang up|thats all|that is all|im done)\b", text):
        return "end_call"
    if re.search(r"\b(about|how long|runtime|rated|rating|synopsis)\b", text):
        return "movie_info"
    return "search"


def _format(text: str) -> tuple[str, str]:
    for pattern, name in _FORMATS:
        if re.search(pattern, text):
            return name, re.sub(pattern, " ", text)
    return "any", text


def _dates(text: str, now: datetime) -> tuple[list[str], str, str, bool]:
    cinema = business_date(now)
    local_day = now.astimezone(NY).date()
    if "this weekend" in text or re.search(r"\bweekend\b", text):
        weekend = resolve_when("this weekend", now)
        return (
            list(weekend.dates),
            weekend.spoken,
            text.replace("this weekend", " ").replace("weekend", " "),
            False,
        )
    nxt = re.search(rf"\bnext\s+({_WEEKDAY})\b", text)
    if nxt:
        following = resolve_when(nxt.group(0), now)
        return (
            list(following.dates),
            following.spoken,
            re.sub(rf"\bnext\s+{nxt.group(1)}\b", " ", text),
            following.unclear,
        )
    if "tomorrow" in text:
        phrase = "tomorrow"
        for part in ("night", "evening", "morning", "afternoon"):
            if f"tomorrow {part}" in text:
                phrase = f"tomorrow {part}"
                break
        when = resolve_when(phrase, now)
        return list(when.dates), when.spoken, text.replace(phrase, " "), False
    if "tonight" in text:
        return [cinema.isoformat()], "tonight", text.replace("tonight", " "), False
    if re.search(r"\btoday\b", text):
        return [cinema.isoformat()], "today", re.sub(r"\btoday\b", " ", text), False
    match = re.search(rf"\b({_WEEKDAY})(?:\s+(night|evening|morning|afternoon))?\b", text)
    if match:
        when = resolve_when(match.group(0), now)
        return (
            list(when.dates),
            when.spoken,
            re.sub(re.escape(match.group(0)), " ", text),
            when.unclear,
        )
    if re.search(r"\b(next|someday|whenever)\b", text):
        return [local_day.isoformat()], "today", text, True
    return [cinema.isoformat()], "today", text, False


def _movie(text: str, titles: list[tuple[str, list[str]]]) -> tuple[str | None, str]:
    best: tuple[int, str, str] | None = None
    for title, aliases in titles:
        for alias in sorted(aliases, key=len, reverse=True):
            needle = _flatten(alias)
            if len(needle) < 3:
                continue
            match = re.search(rf"\b{re.escape(needle)}\b", text)
            if match and (best is None or len(needle) > best[0]):
                best = (len(needle), title, match.group(0))
    if best is None:
        return None, text
    return best[1], text.replace(best[2], " ", 1)


def _location(text: str) -> str | None:
    tokens = [token for token in text.split() if token not in _FILLER and not token.isdigit()]
    if not tokens:
        return None
    return " ".join(tokens)


def tonight_after(now_time: time, requested: time | None, spoken: str) -> time | None:
    """Tonight means after 5 PM, and never a show that already started."""
    if spoken == "tonight":
        floor = time(17, 0)
        if now_time > floor:
            floor = now_time.replace(second=0, microsecond=0)
        if requested is None or requested < floor:
            return floor
    if spoken == "today" and requested is None:
        return now_time.replace(second=0, microsecond=0)
    return requested
