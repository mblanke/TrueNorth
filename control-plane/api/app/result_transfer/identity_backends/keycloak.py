"""Least-privilege Keycloak adapter for identity-free learner accounts."""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .base import (
    BasePseudonymousIdentityBackend,
    OneTimeLogin,
    PseudonymousIdentity,
    PseudonymousIdentityError,
)

ACCOUNT_MARKER = "truenorth-pseudonymous-v1"
USERNAME_PREFIX = "tn-learner-"


@dataclass(frozen=True)
class KeycloakPseudonymousConfig:
    base_url: str
    realm: str
    client_id: str
    client_secret: str = field(repr=False)


def config_from_environment() -> KeycloakPseudonymousConfig:
    base = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    values = {
        "KEYCLOAK_URL": base,
        "KEYCLOAK_ADMIN_CLIENT_ID": os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", "").strip(),
        "KEYCLOAK_ADMIN_CLIENT_SECRET": os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PseudonymousIdentityError(
            "pseudonymous identity provisioning is not configured: " + ", ".join(missing)
        )
    return KeycloakPseudonymousConfig(
        base_url=base,
        realm=os.getenv("KEYCLOAK_REALM", "truenorth").strip() or "truenorth",
        client_id=values["KEYCLOAK_ADMIN_CLIENT_ID"],
        client_secret=values["KEYCLOAK_ADMIN_CLIENT_SECRET"],
    )


class KeycloakPseudonymousIdentityBackend(BasePseudonymousIdentityBackend):
    kind = "keycloak"

    def __init__(
        self,
        config: KeycloakPseudonymousConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or config_from_environment()
        self._transport = transport
        self._admin = f"{self.config.base_url}/admin/realms/{self.config.realm}"

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            with httpx.Client(timeout=30, transport=self._transport) as client:
                token = client.post(
                    f"{self.config.base_url}/realms/{self.config.realm}/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                    },
                )
                token.raise_for_status()
                access_token = token.json()["access_token"]
                response = client.request(
                    method,
                    f"{self._admin}{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    **kwargs,
                )
                response.raise_for_status()
                return response
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise PseudonymousIdentityError(
                f"Keycloak pseudonymous account operation failed ({method} {path.split('?')[0]})"
            ) from exc

    @staticmethod
    def _subject(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PseudonymousIdentityError("Keycloak returned an invalid account subject") from exc

    def _account(self, subject: str) -> dict:
        subject = self._subject(subject)
        response = self._request("GET", f"/users/{subject}")
        try:
            account = response.json()
        except ValueError as exc:
            raise PseudonymousIdentityError("Keycloak returned an invalid account representation") from exc
        marker = (account.get("attributes") or {}).get("tn_account_kind") or []
        username = account.get("username")
        if (
            marker != [ACCOUNT_MARKER]
            or not isinstance(username, str)
            or not username.startswith(USERNAME_PREFIX)
        ):
            raise PseudonymousIdentityError("Keycloak account is not a TrueNorth pseudonymous learner")
        return account

    def _set_enabled(self, subject: str, enabled: bool) -> None:
        subject = self._subject(subject)
        self._request("PUT", f"/users/{subject}", json={"enabled": enabled})

    def create_disabled_account(self) -> PseudonymousIdentity:
        username = f"{USERNAME_PREFIX}{secrets.token_hex(10)}"
        response = self._request(
            "POST",
            "/users",
            json={
                "username": username,
                "enabled": False,
                "emailVerified": False,
                "attributes": {"tn_account_kind": [ACCOUNT_MARKER]},
            },
        )
        location = response.headers.get("Location", "")
        subject = self._subject(urlparse(location).path.rstrip("/").rsplit("/", 1)[-1])
        return PseudonymousIdentity(subject=subject, username=username)

    def prepare_one_time_login(self, subject: str) -> OneTimeLogin:
        account = self._account(subject)
        self._set_enabled(subject, False)
        password = secrets.token_urlsafe(32)
        self._request(
            "PUT",
            f"/users/{self._subject(subject)}/reset-password",
            json={"type": "password", "value": password, "temporary": True},
        )
        return OneTimeLogin(
            subject=self._subject(subject),
            username=account["username"],
            password=password,
        )

    def enable_account(self, subject: str) -> None:
        self._account(subject)
        self._set_enabled(subject, True)

    def disable_account(self, subject: str) -> None:
        self._account(subject)
        self._set_enabled(subject, False)

    def delete_account(self, subject: str) -> None:
        self._account(subject)
        self._request("DELETE", f"/users/{self._subject(subject)}")
