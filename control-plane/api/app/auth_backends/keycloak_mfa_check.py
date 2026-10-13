"""Is the Keycloak realm configured for the MFA the API enforces? Checked at startup.

The API refuses privileged roles without a second factor (app/mfa_policy.py). That only
works when the realm asks for one: the ``tn-browser`` flow is bound (scripts/keycloak_mfa.py),
``acr.loa.map`` names ``mfa`` (the SPA's step-up), and the AMR mapper is on the clients
(otherwise no token says ``otp``). scripts/keycloak_mfa.py writes the realm attribute
``tn.mfa.configured`` after doing all of it; the client scopes themselves are not read here,
because that needs view-clients, which can read client secrets (the API's service account
holds only view-realm and manage-users). A deployment that upgrades the API
without configuring its realm (a Helm chart pointed at an existing Keycloak) would lock
every member of staff out with nothing but 403s. This check names what is missing instead.

``TN_MFA_KEYCLOAK_CHECK``:
  ``refuse`` (default in production): the API does not start while the realm answers and
             lacks any of the above, or cannot be read because the Admin API client
             (KEYCLOAK_ADMIN_CLIENT_ID/SECRET, view-realm) is not configured.
  ``warn``   log the same problems and start. The installer's compose path sets this:
             60-keycloak configures the realm right after the stack starts, and
             95-smoke-test then asserts the flow and mapper (a hard gate there).
  ``off``    (default outside production) no check.
A Keycloak that does not answer (or answers something that is not Keycloak) is logged and
never blocks the start: an outage of the identity provider must not keep the API down after
it recovers. A Keycloak that answers but refuses the admin client (401/403) is a
configuration error, reported like a missing piece (refused in ``refuse`` mode).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("truenorth.mfa")

FLOW_PREFIX = "tn-browser"
CONFIGURED_ATTR = "tn.mfa.configured"


class KeycloakUnreachableError(RuntimeError):
    pass


class AdminClientRefusedError(RuntimeError):
    """Keycloak answered, but refused the Admin API client (wrong secret, disabled, or no
    view-realm): a configuration error, not an outage."""


def mode() -> str:
    from ..settings import is_production

    raw = os.getenv("TN_MFA_KEYCLOAK_CHECK", "").strip().lower()
    if raw in ("refuse", "warn", "off"):
        return raw
    return "refuse" if is_production() else "off"


def _config() -> dict:
    base = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    return {
        "base": base,
        "realm": os.getenv("KEYCLOAK_REALM", "truenorth"),
        "client_id": os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", ""),
        "client_secret": os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", ""),
    }


def realm_problems(realm: dict) -> list[str]:
    """What the realm lacks for MFA, from its representation (view-realm)."""
    problems = []
    flow = realm.get("browserFlow") or ""
    if not flow.startswith(FLOW_PREFIX):
        problems.append(f"browserFlow is {flow or 'unset'!r}, not {FLOW_PREFIX!r} (privileged users would never be asked for OTP)")
    loa = str((realm.get("attributes") or {}).get("acr.loa.map") or "")
    if '"mfa"' not in loa:
        problems.append("the realm attribute acr.loa.map has no \"mfa\" level (step-up cannot work)")
    if not str((realm.get("attributes") or {}).get(CONFIGURED_ATTR) or "").startswith("1:"):
        problems.append(f"the realm attribute {CONFIGURED_ATTR} is missing: scripts/keycloak_mfa.py has not completed "
                        "against this realm (it also installs the AMR mapper, without which no token shows a second factor)")
    return problems


def check() -> list[str]:
    """Problems found (empty: configured). Raises KeycloakUnreachableError when it cannot ask."""
    import httpx

    cfg = _config()
    if not (cfg["base"] and cfg["client_id"] and cfg["client_secret"]):
        return [
            "cannot read the realm: KEYCLOAK_URL, KEYCLOAK_ADMIN_CLIENT_ID and KEYCLOAK_ADMIN_CLIENT_SECRET "
            "(a client whose service account holds realm-management view-realm) are not all set"
        ]
    try:
        with httpx.Client(timeout=10) as client:
            tok = client.post(
                f"{cfg['base']}/realms/{cfg['realm']}/protocol/openid-connect/token",
                data={"grant_type": "client_credentials", "client_id": cfg["client_id"],
                      "client_secret": cfg["client_secret"]},
            )
            if tok.status_code in (400, 401, 403):
                raise AdminClientRefusedError(f"token request answered HTTP {tok.status_code}")
            tok.raise_for_status()
            token = tok.json()["access_token"]
            admin = f"{cfg['base']}/admin/realms/{cfg['realm']}"
            resp = client.get(admin, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (401, 403):
                raise AdminClientRefusedError(f"reading the realm answered HTTP {resp.status_code} (no view-realm?)")
            resp.raise_for_status()
            realm = resp.json()
    except AdminClientRefusedError as exc:
        return [f"misconfigured Admin API client {cfg['client_id']!r}: {exc}; it needs a valid secret and "
                "realm-management view-realm"]
    except httpx.HTTPError as exc:
        raise KeycloakUnreachableError(f"{type(exc).__name__}: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:  # not JSON, or no access_token
        raise KeycloakUnreachableError(f"unexpected answer from Keycloak ({type(exc).__name__})") from exc
    if not isinstance(realm, dict):
        raise KeycloakUnreachableError("unexpected answer from Keycloak (not a realm)")
    return realm_problems(realm)


def enforce_at_startup() -> None:
    """Run the check per TN_MFA_KEYCLOAK_CHECK. Raises settings.UnsafeSettingsError to refuse."""
    from .. import mfa_policy
    from ..settings import UnsafeSettingsError

    current = mode()
    backend = os.getenv("AUTH_BACKEND", "keycloak_oidc").strip().lower()
    if current == "off" or backend != "keycloak_oidc" or not mfa_policy.mfa_roles():
        return
    try:
        problems = check()
    except KeycloakUnreachableError as exc:
        logger.warning("MFA: could not read the Keycloak realm to check its MFA configuration (%s); "
                       "starting anyway", exc)
        return
    if not problems:
        logger.info("MFA: Keycloak realm configured (tn-browser flow, acr mfa, tn.mfa.configured)")
        return
    lines = [f"Keycloak realm is not configured for the MFA this API enforces ({', '.join(sorted(mfa_policy.mfa_roles()))} "
             "would be locked out):", *problems,
             "Fix: run scripts/keycloak_mfa.py against the realm (installer: playbooks/60-keycloak.yml; "
             "Helm: infra/k8s/README.md \"MFA: the chart configures the realm\"; the keycloak-mfa hook), then restart. "
             "TN_MFA_KEYCLOAK_CHECK=warn starts anyway."]
    if current == "refuse":
        raise UnsafeSettingsError(lines)
    for line in lines:
        logger.error("MFA: %s", line)
