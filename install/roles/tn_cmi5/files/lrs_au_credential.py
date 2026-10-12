"""Ensure the cmi5 AU credential exists on the platform's lrsql LRS (roles/tn_cmi5).

Run by the installer inside the api image (`docker compose run ... api python -c`), on the
compose network where the LRS is http://lrs:8080 (LRS_URL). Never prints a secret except a
newly minted credential, on stdout's second line, which the calling shell writes straight
to config/secrets/cmi5_lrs_auth (0600) without echoing it.

Environment:
  LRS_URL, LRS_AUTH                    the api's own (compose.prod.yml)
  LRS_ADMIN_USER, LRS_ADMIN_PASSWORD   the LRS admin account (passed by the installer)
  TN_CMI5_CANDIDATE                    the credential to keep, base64 "key:secret" (or "")
  TN_CMI5_SOURCE                       "vault" (the operator's: never replaced) or "persisted"

stdout line 1, exit 0:
  KEPT       the candidate is a credential of this LRS with exactly the AU scopes
  MINTED     there was no candidate: a new one follows on line 2
  REMINTED   the persisted candidate is unusable here (the LRS lost it, its scopes differ,
             or it is the server's LRS_AUTH): a new one follows on line 2
exit 3, stdout VAULT_REFUSED: the vault's credential is unusable here (reason on stderr).
Anything else (LRS down, bad admin password) raises: exit 1, the installer retries.

Scopes are app.lms.lrsql_admin.AU_SCOPES (tests/contracts/test_installer_cmi5.py holds
them equal): statements write-only, State, Agent and Activity Profiles (docs/cmi5.md).
"""

from __future__ import annotations

import base64
import binascii
import os
import sys

import httpx

AU_SCOPES = ("statements/write", "state", "agents_profile", "activities_profile")


def _split(candidate: str) -> tuple[str, str] | None:
    try:
        key, sep, secret = base64.b64decode(candidate, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return (key, secret) if sep and key and secret else None


def _problem(candidate: str, server_auth: str, creds: list[dict]) -> str | None:
    """Why ``candidate`` cannot be the AU credential on this LRS (None: it can)."""
    if candidate.strip() == server_auth.strip():
        return "it is the server's LRS_AUTH (the LRS could not tell an AU's statements from TrueNorth's)"
    pair = _split(candidate)
    if pair is None:
        return "it is not base64 of key:secret"
    found = next((c for c in creds if (c.get("api-key"), c.get("secret-key")) == pair), None)
    if found is None:
        return "this LRS has no such credential"
    if set(found.get("scopes") or []) != set(AU_SCOPES):
        return f"its scopes are {sorted(found.get('scopes') or [])}, not {sorted(AU_SCOPES)}"
    return None


def ensure(client: httpx.Client, env: dict[str, str]) -> tuple[str, str, str]:
    """(status, new credential or "", reason). See the module docstring."""
    base = env["LRS_URL"].rstrip("/")
    login = client.post(
        f"{base}/admin/account/login",
        json={"username": env["LRS_ADMIN_USER"], "password": env["LRS_ADMIN_PASSWORD"]},
    )
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['json-web-token']}"}
    listed = client.get(f"{base}/admin/creds", headers=headers)
    listed.raise_for_status()

    candidate = env.get("TN_CMI5_CANDIDATE", "").strip()
    reason = ""
    if candidate:
        reason = _problem(candidate, env.get("LRS_AUTH", ""), listed.json()) or ""
        if not reason:
            return "KEPT", "", ""
        if env.get("TN_CMI5_SOURCE") == "vault":
            return "VAULT_REFUSED", "", reason

    made = client.post(f"{base}/admin/creds", json={"scopes": list(AU_SCOPES)}, headers=headers)
    made.raise_for_status()
    cred = made.json()
    new = base64.b64encode(f"{cred['api-key']}:{cred['secret-key']}".encode()).decode()
    return ("REMINTED" if candidate else "MINTED"), new, reason


def main() -> int:
    env = dict(os.environ)
    missing = [k for k in ("LRS_URL", "LRS_ADMIN_USER", "LRS_ADMIN_PASSWORD") if not env.get(k)]
    if missing:
        print(f"not set: {', '.join(missing)}", file=sys.stderr)
        return 2
    with httpx.Client(timeout=15.0) as client:
        status, new, reason = ensure(client, env)
    if reason:
        print(f"cmi5 AU credential: {reason}", file=sys.stderr)
    print(status)
    if new:
        print(new)
    return 3 if status == "VAULT_REFUSED" else 0


if __name__ == "__main__":
    sys.exit(main())
