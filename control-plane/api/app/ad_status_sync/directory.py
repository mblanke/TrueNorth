"""The directory's view of each account, read from Keycloak. Read-only, always.

TrueNorth never writes to Active Directory (ADR 0014 decision 6): Keycloak federates it
``READ_ONLY`` and imports users on its own schedule (full sync hourly, changed users every
5 minutes, install/roles/tn_keycloak). Its ``msad-user-account-control`` mapper (vendor
``ad``) turns AD's ``userAccountControl`` disabled flag into the Keycloak user's
``enabled``; a user removed from AD is removed from Keycloak by its sync; the ``ad-groups``
mapper brings the TN-* group memberships. So this module needs only Keycloak's Admin API,
and only these calls:

- ``POST /realms/<realm>/protocol/openid-connect/token``: the API's client-credentials
  token (the same client as ``/ad-sync``). Authentication, not an admin write.
- ``GET /admin/realms/<realm>/users``: every user, paged (``enabled``, ``federationLink``).
- ``GET /admin/realms/<realm>/groups``: the groups whose name starts with the prefix.
- ``GET /admin/realms/<realm>/groups/<id>/members``: their direct members, paged.

Nothing here sends PUT, POST (other than the token), PATCH or DELETE to Keycloak:
``KeycloakDirectoryReader._get`` is the only request it makes after the token, and the
tests assert on the transport. No LDAP connection either.

Behind ``DirectoryReader`` (ADR 0001) so the service is tested without Keycloak.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field

import httpx

PAGE = 500
# A safety stop, not a capacity: far above any realm this platform serves (~15k VMs).
MAX_PAGES = 400


class DirectoryUnavailableError(Exception):
    """Keycloak could not be read completely. The run stops and changes nothing."""


@dataclass(frozen=True)
class DirectoryUser:
    id: str
    username: str
    email: str
    enabled: bool
    federated: bool  # has a federationLink: an AD account (otherwise a local Keycloak account)


@dataclass
class DirectorySnapshot:
    users: dict[str, DirectoryUser] = field(default_factory=dict)  # by Keycloak id
    # Keycloak ids of the direct members of any group whose name starts with the prefix.
    in_tn_group: set[str] = field(default_factory=set)
    groups_found: list[str] = field(default_factory=list)

    def by_email(self, email: str) -> list[DirectoryUser]:
        e = (email or "").strip().lower()
        return [u for u in self.users.values() if e and u.email.lower() == e]


class DirectoryReader(abc.ABC):
    def ready(self) -> bool:
        """Whether this reader has what it needs to ask the directory at all."""
        return True

    @abc.abstractmethod
    def snapshot(self, group_prefix: str) -> DirectorySnapshot:
        """Every user and the TN-* group members, or ``DirectoryUnavailableError``."""


def keycloak_config() -> dict:
    """The ``/ad-sync`` client's settings (routers/ad_sync.py), read the same way."""
    base = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    if base and not base.endswith("/auth"):
        base = f"{base}/auth"
    return {
        "base": base,
        "realm": os.getenv("KEYCLOAK_REALM", "truenorth"),
        "client_id": os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", ""),
        "client_secret": os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", ""),
    }


def configured() -> bool:
    cfg = keycloak_config()
    return bool(cfg["base"] and cfg["client_id"] and cfg["client_secret"])


class KeycloakDirectoryReader(DirectoryReader):
    def __init__(self, client: httpx.Client | None = None, cfg: dict | None = None):
        self.cfg = cfg or keycloak_config()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=60)
        self._token: str | None = None

    def ready(self) -> bool:
        return bool(self.cfg["base"] and self.cfg["client_id"] and self.cfg["client_secret"])

    def _authenticate(self) -> None:
        resp = self.client.post(
            f"{self.cfg['base']}/realms/{self.cfg['realm']}/protocol/openid-connect/token",
            data={"grant_type": "client_credentials", "client_id": self.cfg["client_id"],
                  "client_secret": self.cfg["client_secret"]},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]

    def _get(self, path: str, params: dict) -> list:
        """The only Admin API request this module makes: a GET."""
        resp = self.client.get(
            f"{self.cfg['base']}/admin/realms/{self.cfg['realm']}{path}",
            params=params, headers={"Authorization": f"Bearer {self._token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            raise DirectoryUnavailableError(f"unexpected answer from {path}")
        return body

    def _paged(self, path: str, **params) -> list:
        out: list = []
        for page in range(MAX_PAGES):
            rows = self._get(path, {**params, "first": page * PAGE, "max": PAGE})
            out += rows
            if len(rows) < PAGE:
                return out
        raise DirectoryUnavailableError(f"{path} has more than {MAX_PAGES * PAGE} entries")

    def snapshot(self, group_prefix: str) -> DirectorySnapshot:
        if not self.ready():
            raise DirectoryUnavailableError("Keycloak Admin API access is not configured (KEYCLOAK_ADMIN_CLIENT_ID)")
        try:
            self._authenticate()
            snap = DirectorySnapshot()
            for u in self._paged("/users", briefRepresentation="false"):
                snap.users[u["id"]] = DirectoryUser(
                    id=u["id"], username=(u.get("username") or "").lower(), email=u.get("email") or "",
                    enabled=bool(u.get("enabled", False)), federated=bool(u.get("federationLink")),
                )
            groups = self._paged("/groups", search=group_prefix, briefRepresentation="true")
            for g in groups:
                name = g.get("name") or ""
                if not name.startswith(group_prefix):
                    continue  # search also matches inside names, and returns parents of matching subgroups
                snap.groups_found.append(name)
                for m in self._paged(f"/groups/{g['id']}/members", briefRepresentation="true"):
                    snap.in_tn_group.add(m["id"])
            return snap
        except DirectoryUnavailableError:
            raise
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise DirectoryUnavailableError(f"Keycloak could not be read: {type(exc).__name__}: {exc}"[:500]) from exc
        finally:
            if self._owns_client:
                self.client.close()
