"""Fuzzy movie-title matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher


def normalize_title(text: str) -> str:
    """Fold years, anniversary tags, and punctuation so one film is one title."""
    lowered = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    lowered = re.sub(r"\([^)]*\b(?:19|20)\d{2}\b[^)]*\)", " ", lowered)
    lowered = re.sub(r"\b(?:19|20)\d{2}\b", " ", lowered)
    lowered = re.sub(r"\b\d+(?:st|nd|rd|th)\s+anniversary\b", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\b(the|a|an)\b", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass(frozen=True)
class TitleHit:
    movie_id: str
    title: str
    score: float


_ROMAN = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}


def sequel_mark(text: str) -> str | None:
    """Trailing number or roman numeral. '2' and 'II' are the same sequel."""
    parts = normalize_title(text).split()
    if not parts:
        return None
    last = parts[-1]
    if last.isdigit():
        return str(int(last))
    return _ROMAN.get(last)


def _stem(text: str) -> str:
    parts = normalize_title(text).split()
    if parts and sequel_mark(text) is not None:
        parts = parts[:-1]
    return " ".join(parts)


def score_title(query: str, candidate: str) -> float:
    if sequel_mark(query) != sequel_mark(candidate):
        return 0.0
    left = _stem(query)
    right = _stem(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if len(left) >= 5 and len(right) >= 5 and _levenshtein(left, right) <= 3:
        ratio = max(ratio, 0.86)
    if left in right or right in left:
        contain = min(len(left), len(right)) / max(len(left), len(right))
        if contain >= 0.75 and min(len(left), len(right)) >= 8:
            ratio = max(ratio, 0.8 + 0.15 * contain)
    return ratio


def best_titles(
    query: str,
    catalog: list[tuple[str, str, list[str]]],
    *,
    limit: int = 3,
) -> list[TitleHit]:
    """`catalog` entries are (movie_id, title, aliases)."""
    hits: list[TitleHit] = []
    for movie_id, title, aliases in catalog:
        names = [title, *aliases]
        score = max(score_title(query, name) for name in names)
        if score >= 0.6:
            hits.append(TitleHit(movie_id, title, score))
    hits.sort(key=lambda hit: (-hit.score, hit.title))
    # One hit per movie.
    seen: set[str] = set()
    unique: list[TitleHit] = []
    for hit in hits:
        if hit.movie_id in seen:
            continue
        seen.add(hit.movie_id)
        unique.append(hit)
    return unique[:limit]


def resolve_title(
    query: str,
    catalog: list[tuple[str, str, list[str]]],
) -> tuple[TitleHit | None, list[TitleHit]]:
    """Return an accepted hit, or None plus did-you-mean suggestions."""
    query_norm = normalize_title(query)
    tokens = query_norm.split()
    short = len(tokens) < 2 and len(query_norm) < 8
    if short:
        word_hits = []
        for movie_id, title, aliases in catalog:
            folded = normalize_title(title)
            alias_set = {normalize_title(alias) for alias in aliases}
            if query_norm == folded or query_norm in alias_set:
                continue
            if query_norm in set(folded.split()):
                word_hits.append(TitleHit(movie_id, title, 0.7))
        if len(word_hits) == 1:
            return None, word_hits
    hits = best_titles(query, catalog, limit=3)
    if not hits:
        return None, []
    top = hits[0]
    second = hits[1].score if len(hits) > 1 else 0.0
    if top.score >= 0.99:
        return top, []
    if short:
        return None, [hit for hit in hits if hit.score >= 0.6]
    if top.score >= 0.9 and top.score - second >= 0.04:
        return top, []
    if top.score >= 0.84 and len(tokens) >= 2 and top.score - second >= 0.08:
        return top, []
    return None, [hit for hit in hits if hit.score >= 0.6]
