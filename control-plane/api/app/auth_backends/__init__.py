"""TrueNorth Range — Auth backend registry and factory.

Usage:
    from app.auth_backends import get_auth_backend

    backend = get_auth_backend()
    payload = await backend.validate_token(raw_token)

Configuration:
    AUTH_BACKEND env var selects the active backend (default: keycloak_oidc).
    AUTH_DISABLED=true overrides to 'disabled' for backward compatibility.

    Supported values:
        keycloak_oidc  — Keycloak OIDC (RS256, JWKS from realm)
        generic_oidc   — Any OIDC IdP (Auth0, Azure AD, Okta, Cognito…)
        disabled       — Dev / offline mode, token validation skipped

Adding a new backend:
    1. Create control-plane/api/app/auth_backends/<name>.py implementing BaseAuthBackend
    2. Add an entry to _REGISTRY below
    3. Set AUTH_BACKEND=<name> in the environment — no other changes required
"""

from __future__ import annotations

import os

from .base import BaseAuthBackend
from .disabled import DisabledAuthBackend
from .generic_oidc import GenericOIDCBackend
from .keycloak_oidc import KeycloakOIDCBackend

__all__ = [
    "BaseAuthBackend",
    "DisabledAuthBackend",
    "GenericOIDCBackend",
    "KeycloakOIDCBackend",
    "get_auth_backend",
]

_REGISTRY: dict[str, type[BaseAuthBackend]] = {
    "keycloak_oidc": KeycloakOIDCBackend,
    "generic_oidc": GenericOIDCBackend,
    "disabled": DisabledAuthBackend,
}

_instance: BaseAuthBackend | None = None


def get_auth_backend() -> BaseAuthBackend:
    """Return the singleton auth backend instance.

    AUTH_DISABLED=true (legacy env var) takes precedence and maps to the
    'disabled' backend so existing deployments are unaffected.

    Raises ValueError for unknown backend names so misconfiguration is
    caught at process startup.
    """
    global _instance
    if _instance is None:
        # Honour legacy AUTH_DISABLED flag
        if os.getenv("AUTH_DISABLED", "false").lower() == "true":
            name = "disabled"
        else:
            name = os.getenv("AUTH_BACKEND", "keycloak_oidc").lower().strip()

        cls = _REGISTRY.get(name)
        if cls is None:
            valid = ", ".join(sorted(_REGISTRY))
            raise ValueError(
                f"Unknown AUTH_BACKEND={name!r}. Valid options: {valid}"
            )
        _instance = cls()
    return _instance


def _reset_backend() -> None:  # pragma: no cover — test helper only
    """Force re-initialisation of the auth backend singleton.

    Call this in tests that need to switch backends between test cases.
    Do not use in production code.
    """
    global _instance
    _instance = None
