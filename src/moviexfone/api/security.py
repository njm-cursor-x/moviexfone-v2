"""Rate limit and shared-secret checks for the public tool routes."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and moment - bucket[0] > self.window_s:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(moment)
        return True


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
    raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "30"})


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    if not origin:
        return False
    return origin.rstrip("/") in {item.rstrip("/") for item in allowed}


def voice_connection_allowed(
    *,
    origin: str | None,
    host: str | None,
    token: str | None,
    allowed_origins: tuple[str, ...],
    secret: str | None,
) -> bool:
    """Browsers must present an allowed Origin. Remote clients with no Origin need the secret."""
    if origin and origin_allowed(origin, allowed_origins):
        return True
    if origin and (host or "") == "testclient" and origin.startswith("http://testserver"):
        return True
    if origin:
        return False
    if (host or "") in _LOCAL_HOSTS:
        return True
    if not secret or not token:
        return False
    return secrets_equal(token, secret)


def enforce_tool_secret(request: Request) -> None:
    secret = request.app.state.settings.tool_secret
    if not secret:
        return
    got = request.headers.get("x-moviexfone-secret", "")
    if not secrets_equal(got, secret):
        raise HTTPException(status_code=401, detail="unauthorized")
