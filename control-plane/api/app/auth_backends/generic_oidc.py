"""TrueNorth Range — Generic OIDC auth backend.

Validates RS256 / ES256 JWTs from any standards-compliant identity
provider: Auth0, Azure AD, Okta, Cognito, Authentik, etc.

Configuration (env vars):
    OIDC_JWKS_URL    — JWKS endpoint URL            (required)
    OIDC_AUDIENCE    — Expected audience claim       (default: "")
    OIDC_ALGORITHMS  — Comma-separated alg list      (default: "RS256")
    OIDC_ISSUER      — Expected issuer (optional)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt

from .base import BaseAuthBackend

logger = logging.getLogger("truenorth.auth.generic_oidc")


class GenericOIDCBackend(BaseAuthBackend):
    """Validates JWTs from any OIDC-compliant identity provider."""

    def __init__(
        self,
        jwks_url: str | None = None,
        audience: str | None = None,
        algorithms: list[str] | None = None,
        issuer: str | None = None,
    ) -> None:
        self._jwks_url = jwks_url or os.getenv("OIDC_JWKS_URL", "")
        if not self._jwks_url:
            raise ValueError(
                "OIDC_JWKS_URL is required for GenericOIDCBackend. "
                "Set it to your IdP's JWKS endpoint (e.g. https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys)."
            )
        self._audience = audience or os.getenv("OIDC_AUDIENCE", "")
        raw_algs = os.getenv("OIDC_ALGORITHMS", "RS256")
        self._algorithms = algorithms or [a.strip() for a in raw_algs.split(",") if a.strip()]
        self._issuer = issuer or os.getenv("OIDC_ISSUER", "") or None
        self._jwks_cache: dict | None = None

    async def _get_jwks(self) -> dict:
        if self._jwks_cache is None:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(self._jwks_url)
                    resp.raise_for_status()
                    self._jwks_cache = resp.json()
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication service temporarily unavailable",
                ) from exc
        return self._jwks_cache

    async def validate_token(self, raw_token: str) -> dict:
        try:
            jwks = await self._get_jwks()
            options: dict = {"verify_aud": bool(self._audience)}
            if self._issuer:
                options["verify_iss"] = True
            return jwt.decode(
                raw_token,
                jwks,
                algorithms=self._algorithms,
                audience=self._audience or None,
                issuer=self._issuer,
                options=options,
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
            ) from None

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(self._jwks_url)
                return resp.status_code < 500
        except Exception:
            return False
