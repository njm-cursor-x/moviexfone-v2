"""Local NYC ZIP centroids and the public MTA station list."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

# Approximate centroids, good enough to land in the right neighborhood.
_ZIPS: dict[str, tuple[float, float, str, str]] = {
    "10001": (40.750, -73.997, "Chelsea", "Manhattan"),
    "10002": (40.716, -73.987, "Lower East Side", "Manhattan"),
    "10003": (40.732, -73.989, "East Village", "Manhattan"),
    "10004": (40.704, -74.013, "Financial District", "Manhattan"),
    "10005": (40.706, -74.009, "Financial District", "Manhattan"),
    "10006": (40.709, -74.013, "Battery Park", "Manhattan"),
    "10007": (40.714, -74.008, "Tribeca", "Manhattan"),
    "10009": (40.727, -73.979, "East Village", "Manhattan"),
    "10010": (40.739, -73.983, "Gramercy", "Manhattan"),
    "10011": (40.742, -74.000, "Chelsea", "Manhattan"),
    "10012": (40.726, -73.998, "SoHo", "Manhattan"),
    "10013": (40.720, -74.005, "Tribeca", "Manhattan"),
    "10014": (40.734, -74.006, "West Village", "Manhattan"),
    "10016": (40.745, -73.978, "Murray Hill", "Manhattan"),
    "10017": (40.752, -73.973, "Midtown East", "Manhattan"),
    "10018": (40.755, -73.993, "Midtown", "Manhattan"),
    "10019": (40.765, -73.985, "Hell's Kitchen", "Manhattan"),
    "10021": (40.769, -73.959, "Upper East Side", "Manhattan"),
    "10023": (40.775, -73.982, "Upper West Side", "Manhattan"),
    "10024": (40.786, -73.974, "Upper West Side", "Manhattan"),
    "10025": (40.799, -73.966, "Upper West Side", "Manhattan"),
    "10026": (40.803, -73.953, "Harlem", "Manhattan"),
    "10027": (40.811, -73.953, "Harlem", "Manhattan"),
    "10028": (40.776, -73.953, "Upper East Side", "Manhattan"),
    "10029": (40.792, -73.944, "East Harlem", "Manhattan"),
    "10030": (40.818, -73.943, "Harlem", "Manhattan"),
    "10031": (40.825, -73.950, "Hamilton Heights", "Manhattan"),
    "10032": (40.838, -73.943, "Washington Heights", "Manhattan"),
    "10033": (40.850, -73.934, "Washington Heights", "Manhattan"),
    "10034": (40.867, -73.922, "Inwood", "Manhattan"),
    "10036": (40.759, -73.990, "Times Square", "Manhattan"),
    "10038": (40.709, -74.003, "Financial District", "Manhattan"),
    "10065": (40.765, -73.963, "Upper East Side", "Manhattan"),
    "10075": (40.773, -73.956, "Upper East Side", "Manhattan"),
    "10128": (40.781, -73.951, "Upper East Side", "Manhattan"),
    "11201": (40.694, -73.990, "Brooklyn Heights", "Brooklyn"),
    "11205": (40.694, -73.966, "Fort Greene", "Brooklyn"),
    "11206": (40.702, -73.942, "Williamsburg", "Brooklyn"),
    "11211": (40.712, -73.953, "Williamsburg", "Brooklyn"),
    "11215": (40.661, -73.986, "Park Slope", "Brooklyn"),
    "11217": (40.681, -73.978, "Boerum Hill", "Brooklyn"),
    "11222": (40.727, -73.951, "Greenpoint", "Brooklyn"),
    "11231": (40.678, -74.001, "Carroll Gardens", "Brooklyn"),
    "11238": (40.679, -73.963, "Prospect Heights", "Brooklyn"),
    "11101": (40.750, -73.940, "Long Island City", "Queens"),
    "11106": (40.764, -73.929, "Astoria", "Queens"),
    "11354": (40.769, -73.830, "Flushing", "Queens"),
    "11375": (40.721, -73.845, "Forest Hills", "Queens"),
    "10451": (40.820, -73.925, "Concourse", "Bronx"),
    "10452": (40.837, -73.923, "Highbridge", "Bronx"),
    "10463": (40.881, -73.906, "Riverdale", "Bronx"),
    "10301": (40.633, -74.096, "St. George", "Staten Island"),
    "10314": (40.599, -74.166, "Bulls Head", "Staten Island"),
}

_ABBREV = {
    "ave": "avenue",
    "av": "avenue",
    "st": "street",
    "blvd": "boulevard",
    "rd": "road",
    "pkwy": "parkway",
    "pl": "place",
    "sq": "square",
}
_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")
_LINE_LETTERS = set("ABCDEFGJLMNQRSZW")
_NUMBER_LINES = {"1", "2", "3", "4", "5", "6", "7"}
_BOROUGHS = {"brooklyn", "manhattan", "queens", "bronx", "staten island", "the bronx"}


def _station_file() -> Path:
    from moviexfone.paths import data_dir

    return data_dir() / "mta-stations.csv"


@lru_cache(maxsize=1)
def stations() -> tuple[tuple[str, float, float, str, frozenset[str]], ...]:
    path = _station_file()
    if not path.is_file():
        return ()
    found: list[tuple[str, float, float, str, frozenset[str]]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            lines = frozenset(char for char in (row.get("lines") or "").upper() if char.isalnum())
            found.append(
                (
                    name,
                    float(row["lat"]),
                    float(row["lng"]),
                    (row.get("borough") or "").strip(),
                    lines,
                )
            )
    return tuple(found)


def _words_and_lines(text: str) -> tuple[tuple[str, ...], frozenset[str]]:
    raw = text.lower().replace("–", " ").replace("—", " ").replace("/", " ").replace("-", " ")
    raw = raw.replace(".", " ")
    raw = _ORDINAL.sub(r"\1", raw)
    words: list[str] = []
    lines: set[str] = set()
    for word in raw.split():
        if word in {"station", "stop", "subway", "train", "the", "at"}:
            continue
        if word in _ABBREV:
            word = _ABBREV[word]
        if len(word) == 1 and word.upper() in _LINE_LETTERS:
            lines.add(word.upper())
            continue
        if word in _NUMBER_LINES:
            lines.add(word)
            continue
        words.append(word)
    return tuple(words), frozenset(lines)


def asks_for_station(query: str) -> bool:
    return bool(re.search(r"\b(station|stop|subway|train)\b", query.lower()))


def is_borough_label(label: str) -> bool:
    return label.strip().lower() in _BOROUGHS


def lookup_local(query: str) -> dict[str, object] | None:
    text = query.strip()
    if re.fullmatch(r"\d{5}", text) and text in _ZIPS:
        lat, lng, label, borough = _ZIPS[text]
        return {"label": label, "lat": lat, "lng": lng, "borough": borough}
    words, lines = _words_and_lines(text)
    if not asks_for_station(text) and not lines:
        return None
    if not words:
        return {"ask": True}
    best: tuple[int, int, tuple[str, float, float, str, frozenset[str]]] | None = None
    for station in stations():
        if lines and not lines <= station[4]:
            continue
        station_words, _station_lines = _words_and_lines(station[0])
        if not station_words:
            continue
        if not set(words) <= set(station_words):
            continue
        extra = len(station_words) - len(words)
        score = len(words) * 10 - extra
        if best is None or score > best[0] or (score == best[0] and extra < best[1]):
            best = (score, extra, station)
    if best is None:
        return {"ask": True} if asks_for_station(text) or bool(lines) else None
    name, lat, lng, borough, _lines = best[2]
    return {"label": name, "lat": lat, "lng": lng, "borough": borough}
