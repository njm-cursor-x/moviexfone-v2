"""HTTP client that can only call the hosts we chose. User text never becomes a URL."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx


class UnsafeURLError(ValueError):
    """The URL is not on the allowlist."""


class AllowedClient:
    def __init__(
        self,
        allow_hosts: set[str],
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        user_agent: str = "MovieXFone/0.1 (local showtimes demo; low volume)",
    ) -> None:
        self._hosts = {host.lower() for host in allow_hosts}
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "application/json, text/html"},
        )

    def close(self) -> None:
        self._client.close()

    def get(
        self, url: str, *, params: dict[str, str] | None = None, max_bytes: int = 8_000_000
    ) -> httpx.Response:
        self._check(url)
        response = self._client.get(url, params=params)
        if len(response.content) > max_bytes:
            raise UnsafeURLError("response too large")
        return response

    def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = 2_000_000,
    ) -> httpx.Response:
        self._check(url)
        response = self._client.post(url, json=payload, headers=headers)
        if len(response.content) > max_bytes:
            raise UnsafeURLError("response too large")
        return response

    def _check(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self._hosts:
            raise UnsafeURLError(f"blocked host: {host or url!r}")
        if parsed.username or parsed.password:
            raise UnsafeURLError("credentials in url")
