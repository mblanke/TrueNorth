"""TrueNorth Range — Abstract auth backend interface.

Any compliant backend must implement this ABC.  The token validation
strategy (Keycloak OIDC, generic OIDC, Azure AD, Okta…) is entirely
the backend's concern; auth.py only interacts with this interface.

Swapping backends is an env-var change (AUTH_BACKEND=<name>), not a
code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAuthBackend(ABC):
    """Minimal interface every auth backend must satisfy."""

    @abstractmethod
    async def validate_token(self, raw_token: str) -> dict:
        """Validate a raw JWT string and return the decoded payload dict.

        Raises fastapi.HTTPException (401) on any validation failure so
        callers do not need to handle individual exception types.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the identity provider is reachable."""
        ...
