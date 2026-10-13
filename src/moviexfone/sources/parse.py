"""Turn upstream payloads into showtime rows. Live feeds and fixtures share these parsers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from moviexfone.sanitize import sanitize_display, sanitize_url
from moviexfone.timeutil import NY, business_date

FORMAT_MAP = {
    "2d-digital": "standard",
    "2d": "standard",
    "digital": "standard",
    "standard": "standard",
    "35mm": "35mm",
    "70mm": "70mm",
    "imax": "imax",
    "dolby": "dolby",
    "dolby-cinema": "dolby",
    "3d": "3d",
    "4dx": "4dx",
    "rpx": "rpx",
    "hdr": "premium_other",
    "open-caption": "standard",
    "unknown": "unknown",
}

ALAMO_CINEMA_IDS = {
    "lower manhattan": "alamo-drafthouse-lower-manhattan",
    "downtown brooklyn": "alamo-drafthouse-downtown-brooklyn",
    "staten island": "alamo-drafthouse-staten-island",
}


@dataclass(frozen=True)
class IncomingShow:
    theater_key: str
    movie_title: str
    starts_at: datetime
    format_norm: str
    format_label: str | None
    status: str
    ticket_url: str | None
    source: str


def _as_dt(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        aware = parsed.replace(tzinfo=NY)
        again = aware.astimezone(ZoneInfo("UTC")).astimezone(NY)
        same = (
            again.year,
            again.month,
            again.day,
            again.hour,
            again.minute,
        ) == (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute)
        if not same:
            return None
        parsed = aware
    return parsed.astimezone(NY)


def _status(raw: str | None) -> str:
    text = (raw or "").strip().lower().replace("-", "").replace("_", "")
    if text in {"onsale", "available"}:
        return "on_sale"
    if text in {"soldout"}:
        return "sold_out"
    if text in {"past", "expired"}:
        return "past"
    if text in {"comingsoon"}:
        return "coming_soon"
    if text in {"notonsale"}:
        return "not_on_sale"
    return "unknown"


def _format(raw: str | None) -> str:
    if not raw:
        return "unknown"
    return FORMAT_MAP.get(raw.strip().lower(), "unknown")


def parse_alamo(payload: dict[str, Any], *, source: str = "alamo_json") -> list[IncomingShow]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    presentations = data.get("presentations") or []
    titles: dict[str, str] = {}
    for item in presentations:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "")
        raw_show = item.get("show")
        show: dict[str, Any] = raw_show if isinstance(raw_show, dict) else {}
        title = sanitize_display(str(show.get("title") or item.get("title") or slug), limit=120)
        if slug and title:
            titles[slug] = title
    cinemas: dict[str, str] = {}
    market = data.get("market") or []
    blocks = market if isinstance(market, list) else [market]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for cinema in block.get("cinemas") or []:
            if not isinstance(cinema, dict):
                continue
            cid = str(cinema.get("id") or "")
            name = str(cinema.get("name") or "")
            mapped = ALAMO_CINEMA_IDS.get(name.strip().lower())
            if cid and mapped:
                cinemas[cid] = mapped
    rows: list[IncomingShow] = []
    for session in data.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        theater_id = cinemas.get(str(session.get("cinemaId") or ""))
        movie_title = titles.get(str(session.get("presentationSlug") or ""))
        starts = _as_dt(str(session.get("showTimeClt") or session.get("showTimeUtc") or ""))
        if not theater_id or not movie_title or starts is None:
            continue
        status = _status(str(session.get("status") or ""))
        if status != "on_sale":
            continue
        fmt_raw = str(session.get("formatSlug") or "")
        session_id = session.get("sessionId") or session.get("id") or ""
        ticket = sanitize_url(
            f"https://drafthouse.com/nyc/show/{session.get('presentationSlug')}"
            f"?cinemaId={session.get('cinemaId')}&sessionId={session_id}"
        )
        rows.append(
            IncomingShow(
                theater_key=theater_id,
                movie_title=movie_title,
                starts_at=starts,
                format_norm=_format(fmt_raw),
                format_label=fmt_raw or None,
                status=status,
                ticket_url=ticket,
                source=source,
            )
        )
    return rows


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            nested = value.get("name") or value.get("title")
            if nested:
                return str(nested)
        elif value:
            return str(value)
    return ""


def parse_assemble(
    payload: dict[str, Any],
    *,
    source: str = "assemble",
    title: str | None = None,
) -> list[IncomingShow]:
    """Accept the live Assemble shape and the older per-screening title shape.

    Live responses put the film id on the payload and omit the title from each
    screening, so the caller passes the title it asked for.
    """
    screenings = _collect_screenings(payload)
    fallback = sanitize_display(title or "", limit=120)
    rows: list[IncomingShow] = []
    for item in screenings:
        title_here = sanitize_display(
            _first(item, ("film", "film_title", "movie", "title", "name")),
            limit=120,
        )
        if not title_here or title_here == sanitize_display(
            _first(item, ("venue", "venue_name", "cinema", "theater", "theatre")),
            limit=120,
        ):
            title_here = fallback
        title = title_here
        venue = sanitize_display(
            _first(item, ("venue", "venue_name", "cinema", "theater", "theatre", "location")),
            limit=120,
        )
        starts = _as_dt(_first(item, ("start", "starts_at", "datetime", "start_time", "time")))
        if not title or not venue or starts is None:
            continue
        status = _status(_first(item, ("availability", "status")) or "on_sale")
        if status not in {"on_sale", "coming_soon"}:
            continue
        ticket = sanitize_url(_first(item, ("tickets_url", "ticket_url", "url", "booking_url")))
        rows.append(
            IncomingShow(
                theater_key=venue,
                movie_title=title,
                starts_at=starts,
                format_norm="unknown",
                format_label=None,
                status=status,
                ticket_url=ticket,
                source=source,
            )
        )
    return rows


def _collect_screenings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("screenings"), list):
        return [item for item in payload["screenings"] if isinstance(item, dict)]
    found: list[dict[str, Any]] = []
    films = payload.get("films")
    if isinstance(films, list):
        for film in films:
            if not isinstance(film, dict):
                continue
            title = film.get("title") or film.get("name")
            for screening in film.get("screenings") or film.get("showtimes") or []:
                if isinstance(screening, dict):
                    row = dict(screening)
                    row.setdefault("film", title)
                    found.append(row)
    result = payload.get("result")
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        nested = json.loads(block["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(nested, dict):
                        found.extend(_collect_screenings(nested))
    return found


_NITEHAWK_SUFFIX = re.compile(
    r"\s+-\s+\d{1,2}/\d{1,2}/\d{2,4}\s*@\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?$",
    re.IGNORECASE,
)


class _JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture = False
        self._buf: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        kind = ""
        for key, value in attrs:
            if key.lower() == "type":
                kind = (value or "").lower()
        self._capture = kind == "application/ld+json"
        self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            self.blocks.append("".join(self._buf))
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)


def parse_nitehawk_html(
    html: str, *, default_theater: str, source: str = "nitehawk_jsonld"
) -> list[IncomingShow]:
    parser = _JsonLdCollector()
    parser.feed(html)
    rows: list[IncomingShow] = []
    for block in parser.blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        flat: list[dict[str, Any]] = []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                flat.extend(item for item in node["@graph"] if isinstance(item, dict))
            elif isinstance(node, dict):
                flat.append(node)
        for node in flat:
            types = node.get("@type")
            type_list = types if isinstance(types, list) else [types]
            if "ScreeningEvent" not in type_list:
                continue
            title = sanitize_display(str(node.get("name") or ""), limit=160)
            title = sanitize_display(_NITEHAWK_SUFFIX.sub("", title), limit=120)
            starts = _as_dt(str(node.get("startDate") or ""))
            if not title or starts is None:
                continue
            raw_location = node.get("location")
            location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
            venue = sanitize_display(str(location.get("name") or default_theater), limit=120)
            url = node.get("url")
            if not url and isinstance(node.get("offers"), dict):
                url = node["offers"].get("url")
            rows.append(
                IncomingShow(
                    theater_key=venue or default_theater,
                    movie_title=title,
                    starts_at=starts,
                    format_norm="unknown",
                    format_label=None,
                    status="on_sale",
                    ticket_url=sanitize_url(str(url) if url else None),
                    source=source,
                )
            )
    return rows


def business_dates_of(rows: list[IncomingShow]) -> set[str]:
    return {business_date(row.starts_at).isoformat() for row in rows}


def local_time(moment: datetime) -> time:
    return moment.astimezone(ZoneInfo("America/New_York")).time().replace(microsecond=0)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def movie_id_for(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return f"movie:{slug[:70]}"
