"""GET-only HTTP for the content feeds (ADR 0014 decision 25, issue #287).

Every feed request goes through ``FeedHttp``: GET, JSON back, through the site's HTTP proxy
when one is configured (``TN_FEEDS_HTTP_PROXY``) and never through anything the
environment happens to set (``trust_env`` off), redirects not followed, the body capped.
Errors name the host and the status, never the query, a header (the NVD key) or the proxy
URL (it may hold a login).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import FeedMalformedError, FeedUnreachableError

USER_AGENT = "TrueNorth-content-feeds/1 (read-only)"
MAX_BYTES = 64 * 1024 * 1024  # the CISA KEV catalogue is ~1.5 MB; NVD pages are ~2 MB


class FeedHttp:
    def __init__(self, *, proxy: str | None = None, timeout: float = 60.0, client: httpx.Client | None = None):
        self.proxy = (proxy or "").strip() or None
        self._client = client or httpx.Client(
            proxy=self.proxy, timeout=timeout, follow_redirects=False, trust_env=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FeedHttp:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> Any:
        host = urlsplit(url).netloc or "the feed"
        try:
            with self._client.stream("GET", url, params=params, headers=headers) as resp:
                if resp.status_code != 200:
                    raise FeedUnreachableError(f"{host} answered HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FeedMalformedError(f"{host} sent more than {MAX_BYTES // (1024 * 1024)} MB")
        except httpx.HTTPError as exc:
            raise FeedUnreachableError(f"{host} could not be reached ({type(exc).__name__})") from None
        try:
            return json.loads(bytes(body))
        except ValueError:
            raise FeedMalformedError(f"{host} did not answer JSON") from None
