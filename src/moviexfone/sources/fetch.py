"""Outbound fetches. URLs are constants. User input is only a query parameter or a title."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from moviexfone.http import AllowedClient, UnsafeURLError
from moviexfone.sources.parse import IncomingShow, parse_alamo, parse_assemble, parse_nitehawk_html

log = logging.getLogger(__name__)

ALAMO_URL = "https://drafthouse.com/s/mother/v2/schedule/market/nyc"
ASSEMBLE_MCP = "https://api.assemble.film/mcp"
NITEHAWK_URLS = {
    "nitehawk-cinema-williamsburg": "https://nitehawkcinema.com/williamsburg",
    "nitehawk-cinema-prospect-park": "https://nitehawkcinema.com/prospectpark",
}
GEO_URL = "https://geosearch.planninglabs.nyc/v2/search"
_CITY_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_TMDB_RE = re.compile(r"^[0-9]{1,10}$")

CITY_SLUGS = {
    "Manhattan": ["new-york-ny"],
    "Brooklyn": ["brooklyn-ny"],
    "Bronx": ["bronx-ny"],
    "Queens": ["astoria-ny", "flushing-ny", "forest-hills-ny"],
    "Staten Island": ["staten-island-ny"],
}
ALL_CITIES = tuple(slug for slugs in CITY_SLUGS.values() for slug in slugs)


def _client() -> AllowedClient:
    return AllowedClient(
        {
            "drafthouse.com",
            "api.assemble.film",
            "nitehawkcinema.com",
            "geosearch.planninglabs.nyc",
        }
    )


def fetch_alamo(client: AllowedClient | None = None) -> list[IncomingShow]:
    own = client is None
    http = client or _client()
    try:
        response = http.get(ALAMO_URL, max_bytes=8_000_000)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return parse_alamo(payload)
    finally:
        if own:
            http.close()


def fetch_nitehawk(client: AllowedClient | None = None) -> list[IncomingShow]:
    own = client is None
    http = client or _client()
    rows: list[IncomingShow] = []
    try:
        for theater_id, url in NITEHAWK_URLS.items():
            response = http.get(url, max_bytes=2_000_000)
            if response.status_code != 200:
                log.warning(
                    "nitehawk fetch failed status=%s theater=%s", response.status_code, theater_id
                )
                continue
            rows.extend(parse_nitehawk_html(response.text, default_theater=theater_id))
        return rows
    finally:
        if own:
            http.close()


def fetch_assemble_city(
    tmdb_id: str,
    city_slug: str,
    *,
    client: AllowedClient | None = None,
) -> dict[str, Any]:
    if not _TMDB_RE.fullmatch(tmdb_id) or not _CITY_RE.fullmatch(city_slug):
        raise UnsafeURLError("bad assemble path")
    own = client is None
    http = client or _client()
    try:
        url = f"https://api.assemble.film/films/{tmdb_id}/screenings"
        response = http.get(url, params={"city": city_slug}, max_bytes=2_000_000)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "5")
            try:
                delay = min(60, max(0, int(retry)))
            except ValueError:
                delay = 5
            log.info("assemble retry-after=%s city=%s", delay, city_slug)
            time.sleep(delay)
            response = http.get(url, params={"city": city_slug}, max_bytes=2_000_000)
        if response.status_code != 200:
            log.info("assemble status=%s city=%s", response.status_code, city_slug)
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    finally:
        if own:
            http.close()


def fetch_assemble_film(
    tmdb_id: str,
    cities: list[str],
    client: AllowedClient | None = None,
    *,
    title: str | None = None,
) -> list[IncomingShow]:
    if not _TMDB_RE.fullmatch(tmdb_id):
        raise UnsafeURLError("bad tmdb id")
    own = client is None
    http = client or _client()
    rows: list[IncomingShow] = []
    try:
        for city in cities:
            if not _CITY_RE.fullmatch(city):
                continue
            payload = fetch_assemble_city(tmdb_id, city, client=http)
            rows.extend(parse_assemble(payload, title=title))
        return rows
    finally:
        if own:
            http.close()


def geosearch(text: str, client: AllowedClient | None = None) -> dict[str, Any] | None:
    """NYC Planning Labs GeoSearch. The text is a query parameter, never a host."""
    query = " ".join(text.split())[:120]
    if not query or "://" in query or query.startswith("//"):
        return None
    own = client is None
    http = client or _client()
    try:
        response = http.get(GEO_URL, params={"text": query, "size": "5"}, max_bytes=500_000)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (UnsafeURLError, httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            http.close()
    if not isinstance(payload, dict):
        return None
    return pick_geosearch(payload, query)


def pick_geosearch(payload: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Prefer a ZIP or neighborhood over a street address with a house number."""
    features = payload.get("features")
    if not isinstance(features, list):
        return None
    ranked: list[tuple[int, dict[str, Any]]] = []
    zip_query = bool(re.fullmatch(r"\d{5}", query.strip()))
    for feature in features:
        if not isinstance(feature, dict):
            continue
        raw_props = feature.get("properties")
        props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
        layer = str(props.get("layer") or "")
        rank = 50
        if zip_query and layer == "postalcode":
            rank = 0
        elif layer in {"neighbourhood", "macrohood", "microhood", "borough", "locality"}:
            rank = 1
        elif props.get("housenumber"):
            rank = 20
        ranked.append((rank, feature))
    ranked.sort(key=lambda item: item[0])
    for _rank, feature in ranked:
        if not isinstance(feature, dict):
            continue
        raw_geometry = feature.get("geometry")
        geometry: dict[str, Any] = raw_geometry if isinstance(raw_geometry, dict) else {}
        coords = geometry.get("coordinates")
        raw_props = feature.get("properties")
        props = raw_props if isinstance(raw_props, dict) else {}
        if not isinstance(coords, list) or len(coords) < 2:
            continue
        borough = props.get("borough") or props.get("borough_gid")
        label = str(props.get("name") or props.get("label") or query)
        return {
            "label": label,
            "lat": float(coords[1]),
            "lng": float(coords[0]),
            "borough": str(borough) if borough else None,
        }
    return None
