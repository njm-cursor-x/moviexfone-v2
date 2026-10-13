from __future__ import annotations

from moviexfone.titles import resolve_title, score_title

CATALOG = [
    ("movie:verity", "Verity", ["verity"]),
    ("movie:digger", "Digger", ["digger", "digga"]),
    ("movie:resident-evil", "Resident Evil", ["resident evil", "the new resident evil"]),
    ("movie:social", "The Social Reckoning", ["the social reckoning", "social reckoning"]),
]


def test_exact_and_article() -> None:
    hit, suggestions = resolve_title("the Verity", CATALOG)
    assert hit is not None and hit.movie_id == "movie:verity"
    assert suggestions == []


def test_asr_miss_digga() -> None:
    hit, _ = resolve_title("Digga", CATALOG)
    assert hit is not None and hit.title == "Digger"


def test_new_resident_evil() -> None:
    hit, _ = resolve_title("the new Resident Evil", CATALOG)
    assert hit is not None and hit.title == "Resident Evil"


def test_unknown_title_offers_nothing_close() -> None:
    hit, suggestions = resolve_title("zzzz-not-a-movie", CATALOG)
    assert hit is None
    assert suggestions == []


def test_close_but_not_exact_does_not_guess() -> None:
    assert score_title("social", "The Social Reckoning") < 1
