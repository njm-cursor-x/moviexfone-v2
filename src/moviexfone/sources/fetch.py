"""Outbound fetches. URLs are constants. User input is only a query parameter or a title."""

from __future__ import annotations

import html
import json
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


def _client(*, timeout: float = 20.0) -> AllowedClient:
    return AllowedClient(
        {
            "drafthouse.com",
            "api.assemble.film",
            "nitehawkcinema.com",
            "geosearch.planninglabs.nyc",
        },
        timeout=timeout,
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


def nitehawk_day_url(base: str, day: object, *, first: bool) -> str:
    stamp = getattr(day, "isoformat", lambda: "")()
    if first or not stamp:
        return base
    return f"{base}/{stamp}/1/"


def fetch_nitehawk(client: AllowedClient | None = None) -> list[IncomingShow]:
    from datetime import timedelta

    own = client is None
    http = client or _client()
    rows: list[IncomingShow] = []
    try:
        for theater_id, url in NITEHAWK_URLS.items():
            from moviexfone.timeutil import NY, now_ny

            start = now_ny().astimezone(NY).date()
            days = [start + timedelta(days=offset) for offset in range(7)]
            for index, day in enumerate(days):
                target = nitehawk_day_url(url, day, first=index == 0)
                if index:
                    time.sleep(0.3)
                response = http.get(target, max_bytes=2_000_000)
                if response.status_code != 200:
                    if index == 0:
                        log.warning(
                            "nitehawk fetch failed status=%s theater=%s",
                            response.status_code,
                            theater_id,
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
    on_retry: Any | None = None,
) -> dict[str, Any]:
    if not _TMDB_RE.fullmatch(tmdb_id) or not _CITY_RE.fullmatch(city_slug):
        raise UnsafeURLError("bad assemble path")
    own = client is None
    http = client or _client(timeout=90.0)
    try:
        url = f"https://api.assemble.film/films/{tmdb_id}/screenings"
        try:
            response = http.get(url, params={"city": city_slug}, max_bytes=2_000_000)
        except httpx.TimeoutException:
            log.info("assemble cold timeout city=%s", city_slug)
            if on_retry is not None:
                on_retry()
            time.sleep(30)
            response = http.get(url, params={"city": city_slug}, max_bytes=2_000_000)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "5")
            try:
                delay = min(60, max(0, int(retry)))
            except ValueError:
                delay = 5
            log.info("assemble retry-after=%s city=%s", delay, city_slug)
            if on_retry is not None:
                on_retry()
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


def normalize_film_query(title: str) -> tuple[str, str | None]:
    """Drop a year or format suffix and unescape HTML before an Assemble search."""
    text = html.unescape(title)
    year_match = re.search(r"\((19|20)\d{2}\)", text)
    year = year_match.group(0)[1:-1] if year_match else None
    text = re.sub(r"\s*\((?:19|20)\d{2}\)", "", text)
    text = re.sub(
        r"\s*\((?:subtitled|dubbed|remastered|extended|director'?s cut)\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text, year


def lookup_film_id(title: str, *, client: AllowedClient) -> str | None:
    """Resolve a catalog title through Assemble's find_film tool. The REST search paths 404."""
    cleaned, year = normalize_film_query(title)
    if not cleaned or len(cleaned) > 120:
        return None
    return _read_film_id(
        client,
        ASSEMBLE_MCP,
        None,
        cleaned,
        year=year,
        post={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_film", "arguments": {"query": cleaned, "title": cleaned}},
        },
    )


def _read_film_id(
    client: AllowedClient,
    url: str,
    params: dict[str, str] | None,
    title: str,
    post: dict[str, object] | None = None,
    year: str | None = None,
) -> str | None:
    try:
        if post is None:
            response = client.get(url, params=params, max_bytes=1_000_000)
        else:
            response = client.post(url, json=post, max_bytes=500_000)
    except (httpx.HTTPError, UnsafeURLError):
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return _film_id_from(_expand_mcp_text(payload), title, year=year)


def _expand_mcp_text(payload: object) -> object:
    """Assemble puts the same matches in content[0].text as a JSON string."""
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    blocks = result.get("content") if isinstance(result, dict) else payload.get("content")
    if not isinstance(blocks, list):
        return payload
    extra: list[object] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.startswith(("{", "[")):
            continue
        try:
            extra.append(json.loads(text))
        except ValueError:
            continue
    if not extra:
        return payload
    return {"payload": payload, "text": extra}


def _film_id_from(payload: object, title: str, *, year: str | None = None) -> str | None:
    wanted = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    found: list[tuple[str, str]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            label = ""
            for key in ("title", "name", "film_title"):
                value = node.get(key)
                if isinstance(value, str):
                    label = value
                    break
            folded = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip() if label else ""
            if folded == wanted:
                film_id = ""
                for key in ("film", "tmdb_id", "film_id", "id"):
                    value = node.get(key)
                    if isinstance(value, (str, int)) and re.fullmatch(r"\d{2,12}", str(value)):
                        film_id = str(value)
                        break
                if film_id:
                    hint = " ".join(
                        str(node.get(key) or "")
                        for key in ("year", "release_year", "playing_from", "release_date")
                    )
                    found.append((film_id, hint))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    if not found:
        return None
    if year:
        for film_id, hint in found:
            if year in hint:
                return film_id
    return found[0][0]


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
        name = str(props.get("name") or "")
        if zip_query and props.get("housenumber") and name.startswith(query.strip()):
            rank = 80
        ranked.append((rank, feature))
    ranked = [item for item in ranked if item[0] < 80]
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
