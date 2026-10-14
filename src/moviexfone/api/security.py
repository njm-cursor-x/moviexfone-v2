"""Rate limit and shared-secret checks for the public tool routes."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from typing import Any

from fastapi import HTTPException, Request

_MAX_KEYS = 4096


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._evict(moment)
            bucket = self._hits.setdefault(key, deque())
            while bucket and moment - bucket[0] > self.window_s:
                bucket.popleft()
            if not bucket and key in self._hits and len(self._hits) > _MAX_KEYS:
                self._hits.pop(key, None)
            if len(bucket) >= self.limit:
                return False
            bucket.append(moment)
            return True

    def _evict(self, moment: float) -> None:
        stale = [
            name
            for name, bucket in self._hits.items()
            if not bucket or moment - bucket[-1] > self.window_s
        ]
        for name in stale:
            self._hits.pop(name, None)
        if len(self._hits) <= _MAX_KEYS:
            return
        oldest = sorted(
            self._hits, key=lambda name: self._hits[name][-1] if self._hits[name] else 0
        )
        for name in oldest[: len(self._hits) - _MAX_KEYS]:
            self._hits.pop(name, None)


def secrets_equal(got: str, expected: str) -> bool:
    left = hashlib.sha256(got.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def enforce_rate_limit(request: Request) -> None:
    limiter: RateLimiter = request.app.state.limiter
    if limiter.allow(client_key(request)):
        return
    window = int(getattr(limiter, "window_s", 60))
    raise HTTPException(
        status_code=429,
        detail="Too many requests",
        headers={"Retry-After": str(window)},
    )


def local_hosts() -> set[str]:
    hosts = {"127.0.0.1", "::1", "localhost"}
    if os.environ.get("MOVIEXFONE_TESTCLIENT") == "1":
        hosts.add("testclient")
    return hosts


def secret_is_strong(secret: str | None) -> bool:
    if not secret:
        return False
    return len(secret.encode("utf-8")) >= 16


def enforce_deployment(settings: Any) -> None:
    """Refuse a public bind or live voice without a real secret."""
    local_bind = settings.bind in {"127.0.0.1", "localhost", "::1"}
    strong = secret_is_strong(settings.tool_secret)
    if not local_bind and not strong:
        raise RuntimeError(
            "Refusing to serve: set MOVIEXFONE_TOOL_SECRET to at least 16 bytes "
            "before binding outside localhost."
        )
    if settings.live_voice and not strong:
        raise RuntimeError(
            "Refusing to serve: MOVIEXFONE_LIVE_VOICE requires MOVIEXFONE_TOOL_SECRET "
            "of at least 16 bytes."
        )


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    if not origin:
        return False
    return origin.rstrip("/") in {item.rstrip("/") for item in allowed}


class VoiceTickets:
    """Single-use, short-lived websocket tickets. The long-lived secret never goes in a URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, float] = {}

    def issue(self, ttl: float = 60.0) -> str:
        nonce = secrets.token_urlsafe(24)
        with self._lock:
            self._drop()
            self._open[nonce] = time.monotonic() + ttl
        return nonce

    def consume(self, nonce: str | None) -> bool:
        if not nonce:
            return False
        with self._lock:
            self._drop()
            expiry = self._open.pop(nonce, None)
        return expiry is not None

    def _drop(self) -> None:
        moment = time.monotonic()
        for key in [item for item, expiry in self._open.items() if expiry < moment]:
            del self._open[key]


def _test_origin(host: str | None, origin: str | None) -> bool:
    return (host or "") == "testclient" and (
        origin is not None and origin.startswith("http://testserver")
    )


def ticket_allowed(
    *,
    origin: str | None,
    host: str | None,
    header_secret: str | None,
    allowed_origins: tuple[str, ...],
    secret: str | None,
) -> bool:
    if origin and (origin_allowed(origin, allowed_origins) or _test_origin(host, origin)):
        return True
    if secret and header_secret and secrets_equal(header_secret, secret):
        return True
    return not origin and (host or "") in local_hosts() and not secret


def voice_connection_allowed(
    *,
    origin: str | None,
    host: str | None,
    token: str | None,
    allowed_origins: tuple[str, ...],
    secret: str | None,
    live: bool = False,
    tickets: VoiceTickets | None = None,
) -> tuple[bool, str]:
    """Reject a foreign Origin. Live and remote sockets need a one-time ticket, not the secret."""
    if origin and not origin_allowed(origin, allowed_origins) and not _test_origin(host, origin):
        return False, "origin not allowed"
    local = (host or "") in local_hosts()
    if live or not local:
        if tickets is not None and tickets.consume(token):
            return True, ""
        return False, "not authorized"
    return True, ""


def enforce_tool_secret(request: Request) -> None:
    settings = request.app.state.settings
    secret = settings.tool_secret
    host = request.client.host if request.client else None
    if not secret:
        if host in local_hosts():
            return
        raise HTTPException(status_code=401, detail="unauthorized")
    got = request.headers.get("x-moviexfone-secret", "")
    if not secrets_equal(got, secret):
        raise HTTPException(status_code=401, detail="unauthorized")
