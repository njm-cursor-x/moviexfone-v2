"""Redact secrets and neutralize instruction-like text from upstream data."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above)\s+instructions?"
    r"|disregard\s+(all\s+|the\s+)?(previous|prior)\s+"
    r"|you\s+are\s+now\s+"
    r"|system\s+prompt"
    r"|<\s*/?\s*system\s*>"
    r"|\[\[.*?\]\])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]{6,}", re.IGNORECASE)
_XAI_KEY = re.compile(r"xai-[A-Za-z0-9_\-]{8,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_display(text: str | None, *, limit: int = 180) -> str:
    """Return a single-line display string safe to put in a tool payload."""
    if not text:
        return ""
    cleaned = _CONTROL.sub(" ", text)
    cleaned = _INJECTION.sub(" ", cleaned)
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
    return raw


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove credentials from log lines."""
    redacted = _BEARER.sub("Bearer [redacted]", text)
    redacted = _XAI_KEY.sub("xai-[redacted]", redacted)
    for secret in secrets or []:
        if secret and len(secret) >= 6 and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
