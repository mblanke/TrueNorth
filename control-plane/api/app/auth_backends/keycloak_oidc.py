"""TrueNorth Range — Keycloak OIDC auth backend.

Validates RS256 JWTs issued by Keycloak, fetching the JWKS from the
realm's well-known endpoint.  Uses the circuit breaker so a Keycloak
outage degrades gracefully.

Configuration (env vars):
    KEYCLOAK_URL       — Keycloak base URL (default: http://keycloak:8080)
    KEYCLOAK_REALM     — Realm name        (default: truenorth)
    KEYCLOAK_AUDIENCE  — If set, the token's aud must include it. Keycloak puts
                         only "account" in aud unless the client has an audience
                         mapper, so add one before setting this (docs/identity.md).
    KEYCLOAK_ISSUER    — If set, the token's iss must equal it. This is the
                         realm's PUBLIC URL as browsers see it, which differs from
                         KEYCLOAK_URL when the API reaches Keycloak internally.

Outside production both checks are off when unset, which is the historical
behaviour: any client in the realm can call the API. Under TN_ENV=production the
audience is required (the backend refuses to build without it, and app/settings.py
refuses to start) and always verified.
"""

from __future__ import annotations

import logging
import os

import httpx
import jwt
from fastapi import HTTPException, status

from .. import jwks as jwks_verify
from ..circuit_breaker import CircuitOpenError, keycloak_breaker
from ..settings import is_production
from .base import BaseAuthBackend
from .jwks_cache import JWKSCache

logger = logging.getLogger("truenorth.auth.keycloak_oidc")


class KeycloakOIDCBackend(BaseAuthBackend):
    """Validates JWTs against a Keycloak OIDC realm."""

    def __init__(
        self,
        keycloak_url: str | None = None,
        realm: str | None = None,
        audience: str | None = None,
        issuer: str | None = None,
    ) -> None:
        self._url = (keycloak_url or os.getenv("KEYCLOAK_URL", "http://keycloak:8080")).rstrip("/")
        self._realm = realm or os.getenv("KEYCLOAK_REALM", "truenorth")
        self._jwks_url = f"{self._url}/realms/{self._realm}/protocol/openid-connect/certs"
        self._audience = audience or os.getenv("KEYCLOAK_AUDIENCE", "").strip() or None
        self._issuer = issuer or os.getenv("KEYCLOAK_ISSUER", "").strip() or None
        self._require_aud = is_production()
        if self._require_aud and not self._audience:
            raise ValueError("KEYCLOAK_AUDIENCE is required when TN_ENV=production (docs/identity.md)")
        self._jwks_cache = JWKSCache(self._fetch_jwks)

    # ------------------------------------------------------------------
    # JWKS helpers (cached in-process; refreshed on TTL and key rotation)
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

    async def _get_jwks(self, kid: str | None = None) -> dict:
        return await self._jwks_cache.get(kid)

    # ------------------------------------------------------------------
    # BaseAuthBackend implementation
    # ------------------------------------------------------------------

    async def validate_token(self, raw_token: str) -> dict:
        try:
            jwks = await self._get_jwks(jwks_verify.unverified_kid(raw_token))
            return jwks_verify.decode(
                raw_token,
                jwks,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                verify_aud=self._require_aud or self._audience is not None,
            )
        except jwt.PyJWTError as exc:
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
