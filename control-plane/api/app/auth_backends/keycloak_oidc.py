"""TrueNorth Range — Keycloak OIDC auth backend.

Validates RS256 JWTs issued by Keycloak, fetching the JWKS from the
realm's well-known endpoint.  Uses the circuit breaker so a Keycloak
outage degrades gracefully.

Configuration (env vars):
    KEYCLOAK_URL    — Keycloak base URL (default: http://keycloak:8080)
    KEYCLOAK_REALM  — Realm name        (default: truenorth)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt

from ..circuit_breaker import CircuitOpenError, keycloak_breaker
from .base import BaseAuthBackend

logger = logging.getLogger("truenorth.auth.keycloak_oidc")


class KeycloakOIDCBackend(BaseAuthBackend):
    """Validates JWTs against a Keycloak OIDC realm."""

    def __init__(
        self,
        keycloak_url: str | None = None,
        realm: str | None = None,
    ) -> None:
        self._url = (keycloak_url or os.getenv("KEYCLOAK_URL", "http://keycloak:8080")).rstrip("/")
        self._realm = realm or os.getenv("KEYCLOAK_REALM", "truenorth")
        self._jwks_url = f"{self._url}/realms/{self._realm}/protocol/openid-connect/certs"
        self._jwks_cache: dict | None = None

    # ------------------------------------------------------------------
    # JWKS helpers (cached in-process)
    # ------------------------------------------------------------------

    async def _fetch_jwks(self) -> dict:
        async def _do_fetch() -> dict:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._jwks_url)
                resp.raise_for_status()
                return resp.json()

        try:
            return await keycloak_breaker.call(_do_fetch)
        except CircuitOpenError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable",
            ) from None

    async def _get_jwks(self) -> dict:
        if self._jwks_cache is None:
            self._jwks_cache = await self._fetch_jwks()
        return self._jwks_cache

    # ------------------------------------------------------------------
    # BaseAuthBackend implementation
    # ------------------------------------------------------------------

    async def validate_token(self, raw_token: str) -> dict:
        try:
            jwks = await self._get_jwks()
            return jwt.decode(
                raw_token,
                jwks,
                algorithms=["RS256"],
                audience="account",
                options={"verify_aud": False},
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
            ) from None

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self._url}/realms/{self._realm}/.well-known/openid-configuration"
                )
                return resp.status_code < 500
        except Exception:
            return False
