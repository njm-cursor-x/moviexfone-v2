"""Trim showtimes into a short spoken answer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time

from moviexfone.timeutil import spoken_time

# At speed 1.15 a caller hears about 2.9 words a second. Ten seconds is ~29 words.
_WORDS_PER_SECOND = 2.875

FORMAT_SPOKEN = {
    "35mm": "35 millimeter",
    "70mm": "70 millimeter",
    "imax": "IMAX",
    "dolby": "Dolby",
    "3d": "3D",
    "4dx": "4DX",
    "rpx": "RPX",
}


@dataclass(frozen=True)
class ShowCard:
    theater: str
    theater_id: str
    movie: str
    movie_id: str
    distance_mi: float | None
    times_local: list[time]
    format_norm: str
    ticket_url: str | None
    source: str
    borough: str | None = None
    day_labels: list[str] = field(default_factory=list)


def estimate_spoken_seconds(text: str) -> float:
    words = re.findall(r"[A-Za-z0-9']+", text)
    if not words:
        return 0.0
    return round(len(words) / _WORDS_PER_SECOND, 2)


def _join_times(words: list[str]) -> str:
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return ", ".join(words[:-1]) + f", and {words[-1]}"


def _format_phrase(fmt: str) -> str:
    spoken = FORMAT_SPOKEN.get(fmt)
    if not spoken:
        return ""
    return f" in {spoken}"


_DAY_START = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|next)\b")


def _starts_with_day(clock: str) -> bool:
    return bool(_DAY_START.match(clock))


def spoken_title(title: str) -> str:
    """Drop a year suffix so the voice does not say "(2026)"."""
    return re.sub(r"\s*\((?:19|20)\d{2}\)", "", title).strip()


def _drop_sentence_period(text: str) -> str:
    """Remove a sentence period without turning "a.m." into "a.m"."""
    if re.search(r"[ap]\.m\.$", text):
        return text
    if text.endswith("."):
        return text[:-1]
    return text


def _sentence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.endswith("."):
        return cleaned
    return f"{cleaned}."


def _clocks(card: ShowCard, max_times: int, name_days: bool) -> str:
    moments = card.times_local[:max_times]
    words = [spoken_time(moment) for moment in moments]
    labels = card.day_labels[: len(words)]
    if name_days and labels and len(set(labels)) == 1:
        return f"{labels[0]} at {_join_times(words)}"
    if name_days and labels and len(labels) == len(words):
        return _join_times([f"{label} {word}" for label, word in zip(labels, words, strict=False)])
    return _join_times(words)


def render_speak(
    cards: list[ShowCard],
    *,
    when: str,
    more: int,
    max_times: int,
    specific_movie: bool,
    name_days: bool = False,
    name_theater: bool = False,
) -> str:
    parts: list[str] = []
    same_theater = len({card.theater for card in cards}) == 1 and len(cards) > 1
    for index, card in enumerate(cards):
        clock = _clocks(card, max_times, name_days)
        movie = spoken_title(card.movie)
        fmt = _format_phrase(card.format_norm)
        link = "on" if _starts_with_day(clock) else "at"
        if specific_movie:
            if index == 0:
                parts.append(_sentence(f"{when} at {card.theater}, {movie}{fmt} {link} {clock}"))
            else:
                parts.append(_sentence(f"At {card.theater}, {clock}"))
        elif same_theater:
            place = "" if name_theater else f" at {card.theater}"
            if index == 0:
                parts.append(f"{when}{place}: {movie}{fmt} {link} {clock}")
            elif index == len(cards) - 1:
                parts.append(_sentence(f"and {movie}{fmt} {link} {clock}"))
            else:
                parts.append(f"{movie}{fmt} {link} {clock}")
        elif index == 0:
            parts.append(_sentence(f"{when}: {movie} at {card.theater}{fmt} {link} {clock}"))
        else:
            parts.append(_sentence(f"{movie} at {card.theater}{fmt} {link} {clock}"))
    if same_theater and parts:
        text = " ".join(parts)
        if not text.endswith("."):
            text += "."
    else:
        text = " ".join(parts)
    if more > 0:
        text = f"{text} Want more?"
    return re.sub(r"\s+", " ", text).strip()


def trim_cards(
    cards: list[ShowCard],
    *,
    page: int,
    max_cards: int,
    max_times: int,
    when: str,
    specific_movie: bool,
    second_budget: float = 10.5,
    name_days: bool = False,
    name_theater: bool = False,
    prefix: str = "",
) -> tuple[list[ShowCard], int, str]:
    """Return the page of cards, how many remain, and a short `speak` line."""
    start = max(0, (page - 1) * max_cards)
    window = cards[start:]
    more_after = max(0, len(window) - max_cards)
    chosen = window[:max_cards]
    times = max_times

    def _speak(picked: list[ShowCard], remaining: int, limit: int) -> str:
        line = render_speak(
            picked,
            when=when,
            more=remaining,
            max_times=limit,
            specific_movie=specific_movie,
            name_days=name_days,
            name_theater=name_theater,
        )
        if prefix:
            return f"{prefix} {line}".strip()
        return line

    speak = _speak(chosen, more_after, times)
    if estimate_spoken_seconds(speak) > second_budget and times > 2:
        times = 2
        speak = _speak(chosen, more_after, times)
    if estimate_spoken_seconds(speak) > second_budget and len(chosen) > 1:
        extra = more_after + (len(chosen) - 1)
        chosen = chosen[:1]
        times = min(times, 2)
        speak = _speak(chosen, extra, times)
        more_after = extra
    hidden = sum(max(0, len(card.times_local) - times) for card in chosen)
    if hidden:
        want = ""
        base = speak
        if base.endswith("Want more?"):
            base = base[: -len("Want more?")].rstrip()
            want = " Want more?"
        base = _drop_sentence_period(base)
        with_more = f"{base}, and {hidden} more.{want}"
        if specific_movie or estimate_spoken_seconds(with_more) <= second_budget:
            speak = with_more
    trimmed: list[ShowCard] = []
    for card in chosen:
        trimmed.append(
            ShowCard(
                theater=card.theater,
                theater_id=card.theater_id,
                movie=card.movie,
                movie_id=card.movie_id,
                distance_mi=card.distance_mi,
                times_local=list(card.times_local[:times]),
                format_norm=card.format_norm,
                ticket_url=card.ticket_url,
                source=card.source,
                borough=card.borough,
                day_labels=list(card.day_labels[:times]),
            )
        )
    return trimmed, more_after, speak
