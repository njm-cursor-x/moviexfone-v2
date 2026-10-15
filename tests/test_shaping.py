from __future__ import annotations

from datetime import time

from moviexfone.shaping import ShowCard, estimate_spoken_seconds, trim_cards


def _card(theater: str, movie: str, hours: list[int]) -> ShowCard:
    return ShowCard(
        theater=theater,
        theater_id=theater.lower().replace(" ", "-"),
        movie=movie,
        movie_id=movie.lower(),
        distance_mi=0.2,
        times_local=[time(hour, 10) for hour in hours],
        format_norm="unknown",
        ticket_url="https://example.com/tickets",
        source="assemble",
        borough="Manhattan",
    )


def test_trim_keeps_two_cards_and_three_times() -> None:
    cards = [
        _card("Regal Union Square", "Digger", [19, 20, 21, 22, 23]),
        _card("AMC 19th Street", "Digger", [18, 19, 20, 21]),
        _card("Quad Cinema", "Digger", [19, 21]),
    ]
    chosen, more, speak = trim_cards(
        cards,
        page=1,
        max_cards=2,
        max_times=3,
        when="Tonight",
        specific_movie=True,
    )
    assert len(chosen) == 2
    assert all(len(card.times_local) <= 3 for card in chosen)
    assert more == 1
    assert estimate_spoken_seconds(speak) <= 10.5
    assert "Want more?" in speak


def test_broad_answer_stays_short() -> None:
    cards = [_card(f"Theater {i}", f"Movie {i}", [19, 21, 23]) for i in range(6)]
    chosen, more, speak = trim_cards(
        cards,
        page=1,
        max_cards=2,
        max_times=2,
        when="Tonight near Union Square",
        specific_movie=False,
    )
    assert len(chosen) <= 2
    assert more >= 1
    assert estimate_spoken_seconds(speak) <= 10.5
    assert "http" not in speak
