"""Trim showtimes into a short spoken answer."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


def render_speak(
    cards: list[ShowCard],
    *,
    when: str,
    more: int,
    max_times: int,
    specific_movie: bool,
) -> str:
    parts: list[str] = []
    same_theater = len({card.theater for card in cards}) == 1 and len(cards) > 1
    for index, card in enumerate(cards):
        words = [spoken_time(moment) for moment in card.times_local[:max_times]]
        clock = _join_times(words)
        fmt = _format_phrase(card.format_norm)
        if specific_movie:
            if index == 0:
                parts.append(f"{when} at {card.theater}, {card.movie}{fmt} at {clock}.")
            else:
                parts.append(f"At {card.theater}, {clock}.")
        elif same_theater:
            if index == 0:
                parts.append(f"{when}: {card.movie}{fmt} at {clock}")
            elif index == len(cards) - 1:
                parts.append(f"and {card.movie}{fmt} at {clock}.")
            else:
                parts.append(f"{card.movie}{fmt} at {clock}")
        elif index == 0:
            parts.append(f"{when}: {card.movie} at {card.theater}{fmt} at {clock}.")
        else:
            parts.append(f"{card.movie} at {card.theater}{fmt} at {clock}.")
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
) -> tuple[list[ShowCard], int, str]:
    """Return the page of cards, how many remain, and a short `speak` line."""
    start = max(0, (page - 1) * max_cards)
    window = cards[start:]
    more_after = max(0, len(window) - max_cards)
    chosen = window[:max_cards]
    times = max_times

    def _speak(picked: list[ShowCard], remaining: int, limit: int) -> str:
        return render_speak(
            picked,
            when=when,
            more=remaining,
            max_times=limit,
            specific_movie=specific_movie,
        )

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
            )
        )
    return trimmed, more_after, speak
