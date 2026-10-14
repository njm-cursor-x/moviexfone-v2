"""Resolve a caller's place words to a theater, a chain, or a point in New York."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from moviexfone.geo import borough_of

Geocoder = Callable[[str], dict[str, object] | None]


@dataclass
class TheaterRef:
    theater_id: str
    name: str
    spoken_name: str
    operator: str
    borough: str
    lat: float | None
    lng: float | None
    website: str | None
    status: str
    covered: bool
    aliases: list[str]


@dataclass
class PlaceRef:
    place_id: str
    name: str
    kind: str
    borough: str | None
    lat: float | None
    lng: float | None
    radius_mi: float
    aliases: list[str]


@dataclass
class Resolution:
    kind: str
    label: str
    lat: float | None = None
    lng: float | None = None
    radius_mi: float | None = None
    borough: str | None = None
    theater_ids: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    website: str | None = None
    covered: bool | None = None


def normalize_place(text: str) -> str:
    lowered = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    lowered = re.sub(r"^(the|a|an)\s+", "", lowered)
    return lowered


def score_place(query: str, alias: str) -> float:
    left = normalize_place(query)
    right = normalize_place(alias)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if left_tokens and left_tokens <= right_tokens:
        cover = len(left_tokens) / len(right_tokens)
        short = len(left_tokens) == 1 and len(left) < 8
        if not short and cover >= 0.67:
            return 0.8 + 0.15 * cover
    if right.startswith(left) and len(left) >= 4:
        return 0.86
    ratio = SequenceMatcher(None, left, right).ratio()
    return ratio if ratio >= 0.88 else 0.0


def resolve_location(
    query: str,
    theaters: list[TheaterRef],
    places: list[PlaceRef],
    *,
    geocode: Geocoder | None = None,
) -> Resolution:
    folded = normalize_place(query)
    if folded in {"new york", "nyc", "new york city"}:
        return Resolution(
            kind="not_found",
            label="New York",
            options=[],
        )
    named = [place for place in places if folded == normalize_place(place.name)]
    if len(named) == 1:
        place = named[0]
        return Resolution(
            kind="borough" if place.kind == "borough" else "place",
            label=place.name,
            lat=place.lat,
            lng=place.lng,
            radius_mi=place.radius_mi,
            borough=place.borough,
        )
    local = _local_place(query)
    if local is not None:
        return local
    exact_place_name = any(folded == normalize_place(place.name) for place in places)
    if len(folded.split()) == 1 and len(folded) >= 4 and not exact_place_name:
        mentions: list[tuple[str, str]] = []
        for theater in theaters:
            if theater.status != "open":
                continue
            blob = " ".join([theater.spoken_name, *theater.aliases])
            if folded in set(normalize_place(blob).split()):
                mentions.append(("theater", theater.spoken_name))
        for place in places:
            blob = " ".join([place.name, *place.aliases])
            if folded in set(normalize_place(blob).split()):
                mentions.append(("place", place.name))
        mention_labels = {label for _kind, label in mentions}
        mixed = any(kind == "place" for kind, _label in mentions) and any(
            kind == "theater" for kind, _label in mentions
        )
        if mixed and len(mention_labels) >= 3:
            return _ambiguous(sorted(mention_labels))
    scored: list[tuple[float, str, TheaterRef | PlaceRef]] = []
    for theater in theaters:
        if theater.status != "open":
            continue
        names = [theater.spoken_name, *theater.aliases]
        if "(" in query:
            names.append(theater.name)
        else:
            names = [name for name in names if name and "(" not in name]
        score = max(score_place(query, name) for name in names if name)
        if score >= 0.8:
            scored.append((score, "theater", theater))
    for place in places:
        score = max(score_place(query, name) for name in [place.name, *place.aliases])
        if score >= 0.8:
            scored.append((score, "place", place))
    if not scored:
        return _from_geocode(query, geocode)
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][0]
    query_tokens = folded.split()
    if len(query_tokens) == 1:
        broad = [item for item in scored if item[0] >= 0.8]
        broad_labels = {_label(item) for item in broad}
        mixed = any(item[1] == "place" for item in broad) and any(
            item[1] == "theater" for item in broad
        )
        if mixed and len(broad_labels) >= 3:
            return _ambiguous(sorted(broad_labels))
    close = [item for item in scored if best - item[0] <= 0.08]
    theaters_close = [item for item in close if item[1] == "theater"]
    places_close = [item for item in close if item[1] == "place"]
    if len(close) == 1:
        return _single(close[0])
    if theaters_close and not places_close:
        return _theaters_only(query, theaters_close)
    if places_close and not theaters_close:
        if len(places_close) == 1:
            return _single(places_close[0])
        return _ambiguous([_label(item) for item in places_close])
    exact_theaters = [item for item in theaters_close if item[0] >= 0.99]
    exact_places = [item for item in places_close if item[0] >= 0.99]
    if len(exact_theaters) == 1 and not exact_places:
        return _single(exact_theaters[0])
    if len(exact_places) == 1 and not exact_theaters:
        return _single(exact_places[0])
    if exact_theaters and not exact_places:
        return _theaters_only(query, exact_theaters)
    labels = [_label(item) for item in close]
    return _ambiguous(labels)


def _label(item: tuple[float, str, TheaterRef | PlaceRef]) -> str:
    obj = item[2]
    if isinstance(obj, TheaterRef):
        return obj.spoken_name or obj.name
    borough = f", {obj.borough}" if obj.borough and obj.kind != "borough" else ""
    return f"{obj.name}{borough}"


def _single(item: tuple[float, str, TheaterRef | PlaceRef]) -> Resolution:
    obj = item[2]
    if isinstance(obj, TheaterRef):
        return Resolution(
            kind="theater",
            label=obj.spoken_name or obj.name,
            lat=obj.lat,
            lng=obj.lng,
            borough=obj.borough,
            theater_ids=[obj.theater_id],
            website=obj.website,
            covered=obj.covered,
        )
    if obj.kind == "borough":
        return Resolution(kind="borough", label=obj.name, borough=obj.name)
    return Resolution(
        kind="place",
        label=obj.name,
        lat=obj.lat,
        lng=obj.lng,
        radius_mi=obj.radius_mi,
        borough=obj.borough,
    )


def _theaters_only(
    query: str,
    items: list[tuple[float, str, TheaterRef | PlaceRef]],
) -> Resolution:
    theaters = [item[2] for item in items if isinstance(item[2], TheaterRef)]
    if len(theaters) == 1:
        return _single(items[0])
    operators = {theater.operator for theater in theaters}
    query_norm = normalize_place(query)
    shared_alias = all(
        any(normalize_place(alias) == query_norm for alias in theater.aliases)
        for theater in theaters
    )
    operator_hit = len(operators) == 1 and query_norm in normalize_place(next(iter(operators)))
    if len(operators) == 1 and (shared_alias or operator_hit):
        operator = next(iter(operators))
        short = operator
        for token in ("Alamo", "Nitehawk", "AMC", "Regal"):
            if token.lower() in operator.lower():
                short = token
                break
        return Resolution(
            kind="chain",
            label=short,
            theater_ids=[theater.theater_id for theater in theaters],
            borough=None,
        )
    return _ambiguous([theater.spoken_name or theater.name for theater in theaters])


def _ambiguous(options: list[str]) -> Resolution:
    unique: list[str] = []
    for option in options:
        if option not in unique:
            unique.append(option)
    return Resolution(
        kind="ambiguous",
        label="ambiguous",
        options=unique[:4],
    )


def _local_place(query: str) -> Resolution | None:
    from moviexfone.localgeo import lookup_local

    hit = lookup_local(query)
    if hit is None:
        return None
    if hit.get("ask"):
        return Resolution(kind="not_found", label=query.strip())
    lat = hit["lat"]
    lng = hit["lng"]
    if not isinstance(lat, float) or not isinstance(lng, float):
        return None
    return Resolution(
        kind="place",
        label=str(hit["label"]),
        lat=lat,
        lng=lng,
        radius_mi=0.6,
        borough=str(hit["borough"]) if hit.get("borough") else None,
    )


def _from_geocode(query: str, geocode: Geocoder | None) -> Resolution:
    if geocode is None or "://" in query or query.strip().startswith("//"):
        return Resolution(kind="not_found", label=query.strip())
    hit = geocode(query)
    if not hit:
        return Resolution(kind="not_found", label=query.strip())
    lat_raw = hit.get("lat")
    lng_raw = hit.get("lng")
    if not isinstance(lat_raw, (int, float)) or not isinstance(lng_raw, (int, float)):
        return Resolution(kind="not_found", label=query.strip())
    lat = float(lat_raw)
    lng = float(lng_raw)
    borough = borough_of(lat, lng)
    if borough is None:
        return Resolution(kind="not_found", label=query.strip())
    raw_label = str(hit.get("label") or query.strip())
    from moviexfone.localgeo import is_borough_label

    if is_borough_label(raw_label) or is_borough_label(query):
        return Resolution(kind="not_found", label=query.strip())
    letters = [char for char in raw_label if char.isalpha()]
    upper = sum(char.isupper() for char in letters)
    if letters and upper / len(letters) > 0.7:
        label = borough or query.strip()
    elif query.strip().isdigit() and raw_label.startswith(query.strip()):
        label = borough or query.strip()
    else:
        label = raw_label
    return Resolution(
        kind="place",
        label=label,
        lat=lat,
        lng=lng,
        radius_mi=0.8,
        borough=borough,
    )
