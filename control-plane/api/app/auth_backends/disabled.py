"""TrueNorth Range — Disabled auth backend (dev / offline mode).

Used when AUTH_DISABLED=true or AUTH_BACKEND=disabled.

validate_token should never be called in disabled mode (the facade
short-circuits before reaching it), but raises clearly if it is.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from .base import BaseAuthBackend


class DisabledAuthBackend(BaseAuthBackend):
    """No-op auth backend for development and offline ranges."""

    async def validate_token(self, raw_token: str) -> dict:  # pragma: no cover
        # auth.py returns the dev user before calling validate_token when
        # AUTH_DISABLED=true.  This path is a safety net only.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="validate_token called on DisabledAuthBackend — this is a bug",
        )

    async def health_check(self) -> bool:
        return True
