"""Redact secrets and neutralize instruction-like text from upstream data."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_INJECTION = re.compile(
    r"(ignore\b.{0,40}\binstructions?"
    r"|disregard\b"
    r"|you\s+are\s+now\b"
    r"|you\s+must\b"
    r"|system\s+prompt"
    r"|tell\s+the\s+caller\b"
    r"|<\s*/?\s*script\b"
    r"|<\s*/?\s*system\s*>"
    r"|\[\[)",
    re.IGNORECASE,
)
_TICKET_HOSTS = (
    "drafthouse.com",
    "nitehawkcinema.com",
    "amctheatres.com",
    "regmovies.com",
    "fandango.com",
    "atomtickets.com",
    "angelikafilmcenter.com",
    "ifccenter.com",
    "filmforum.org",
    "metrograph.com",
    "quadcinema.com",
    "cinemavillage.com",
    "film-linc.org",
    "paristheaternyc.com",
    "villageeast.com",
    "thecinemasnewyork.com",
    "cobblehillcinemas.com",
    "alpinecinemas.com",
    "citycinemas.com",
    "landmarktheatres.com",
)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]{6,}", re.IGNORECASE)
_XAI_KEY = re.compile(r"xai-[A-Za-z0-9_\-]{8,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_display(text: str | None, *, limit: int = 180) -> str:
    """Return a single-line display string safe to put in a tool payload."""
    if not text:
        return ""
    cleaned = html.unescape(_INVISIBLE.sub("", text))
    cleaned = _CONTROL.sub(" ", cleaned)
    cleaned = re.sub(r"<\s*script\b[^>]*>.*?</\s*script\s*>", " ", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<[^>]*>", " ", cleaned)
    kept = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", cleaned)
        if part.strip() and not _INJECTION.search(part)
    ]
    cleaned = " ".join(kept)
    if not cleaned or _INJECTION.search(cleaned):
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def sanitize_url(url: str | None) -> str | None:
    """Keep http(s) ticket links. Never treat them as something we should fetch."""
    if not url:
        return None
    raw = url.strip()
    if len(raw) > 500 or any(ch.isspace() for ch in raw):
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in _TICKET_HOSTS):
        return None
    return raw


_QUERY_SECRET = re.compile(r"((?:token|ticket)=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove credentials from log lines."""
    redacted = _QUERY_SECRET.sub(r"\1[redacted]", text)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _XAI_KEY.sub("xai-[redacted]", redacted)
    for secret in secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
