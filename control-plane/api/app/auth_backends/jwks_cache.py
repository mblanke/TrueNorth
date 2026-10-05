"""A JWKS cache that survives signing-key rotation.

The OIDC backends are process-wide singletons and used to fetch the JWKS once and keep
it forever. When the IdP rotated its signing key, every new token named a kid the API
had never seen, and all logins failed until the API was restarted.

- Refresh after `ttl` seconds.
- Refresh early when a token names an unknown kid (that is what rotation looks like),
  but at most once per `min_refresh_interval`, so unauthenticated callers sending
  random kids cannot turn the API into a JWKS-fetching amplifier against the IdP.
- If a refresh fails and keys are already cached, keep serving them: an IdP blip
  should not log everyone out. With nothing cached, the fetch error propagates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("truenorth.auth.jwks")

DEFAULT_TTL = float(os.getenv("JWKS_CACHE_TTL_SECONDS", "600"))
DEFAULT_MIN_REFRESH = float(os.getenv("JWKS_MIN_REFRESH_SECONDS", "30"))


class JWKSCache:
    def __init__(
        self,
        fetch: Callable[[], Awaitable[Any]],
        *,
        ttl: float = DEFAULT_TTL,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._min_refresh = min_refresh_interval
        self._clock = clock
        self._jwks: Any = None
        self._kids: set[str] = set()
        self._fetched_at = 0.0
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def get(self, kid: str | None = None) -> Any:
        """The current JWKS, refreshed if stale or if `kid` is not in it."""
        if self._needs_refresh(kid):
            async with self._lock:
                if self._needs_refresh(kid):  # another task may have refreshed meanwhile
                    await self._refresh()
        return self._jwks

    def _needs_refresh(self, kid: str | None) -> bool:
        if self._jwks is None:
            return True
        now = self._clock()
        throttled = self._last_attempt is not None and now - self._last_attempt < self._min_refresh
        if throttled:
            return False
        expired = now - self._fetched_at >= self._ttl
        unknown_kid = isinstance(kid, str) and kid not in self._kids
        return expired or unknown_kid

    async def _refresh(self) -> None:
        self._last_attempt = self._clock()
        try:
            jwks = await self._fetch()
        except Exception:
            if self._jwks is None:
                raise
            logger.warning("JWKS refresh failed; keeping the %d cached key(s)", len(self._kids), exc_info=True)
            return
        self._jwks = jwks
        self._kids = _kids_of(jwks)
        self._fetched_at = self._clock()


def _kids_of(jwks: Any) -> set[str]:
    keys = jwks.get("keys", [jwks]) if isinstance(jwks, dict) else jwks
    if not isinstance(keys, list):
        return set()
    return {k["kid"] for k in keys if isinstance(k, dict) and isinstance(k.get("kid"), str)}
