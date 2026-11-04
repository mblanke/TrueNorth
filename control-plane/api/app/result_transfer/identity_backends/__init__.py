"""Registry for pseudonymous identity providers (ADR 0001)."""

from __future__ import annotations

import os

from .base import (
    BasePseudonymousIdentityBackend,
    OneTimeLogin,
    PseudonymousIdentity,
    PseudonymousIdentityError,
)
from .keycloak import KeycloakPseudonymousIdentityBackend

_REGISTRY: dict[str, type[BasePseudonymousIdentityBackend]] = {
    KeycloakPseudonymousIdentityBackend.kind: KeycloakPseudonymousIdentityBackend,
}


def get_pseudonymous_identity_backend(
    kind: str | None = None,
    **kwargs: object,
) -> BasePseudonymousIdentityBackend:
    selected = kind or os.getenv("PSEUDONYMOUS_IDENTITY_BACKEND", "keycloak").strip()
    cls = _REGISTRY.get(selected)
    if cls is None:
        raise ValueError(
            f"unsupported pseudonymous identity backend {selected!r}; one of {sorted(_REGISTRY)}"
        )
    return cls(**kwargs)


__all__ = [
    "BasePseudonymousIdentityBackend",
    "OneTimeLogin",
    "PseudonymousIdentity",
    "PseudonymousIdentityError",
    "get_pseudonymous_identity_backend",
]
