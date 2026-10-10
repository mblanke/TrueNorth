"""Mint the cmi5 AU credential on a yetanalytics/lrsql LRS (its admin API).

cmi5 AU traffic is forwarded with its own LRS credential, ``CMI5_LRS_AUTH``, never the
server's ``LRS_AUTH``: the LRS stamps every statement's ``authority`` with the credential
that wrote it, so a statement an AU sent can always be told from one TrueNorth's server
wrote (docs/cmi5.md). The credential is also least-privilege: statements write-only (lrsql
answers 403 to a read), State, Agent and Activity Profile documents.

    python -m app.lms.lrsql_admin            # prints CMI5_LRS_AUTH=<base64 key:secret>

Reads LRS_URL, LRS_ADMIN_USER and LRS_ADMIN_PASSWORD from the environment. lrsql-specific,
so it lives with the LRS adapter.
"""

from __future__ import annotations

import base64
import os
import sys

import httpx

AU_SCOPES = ("statements/write", "state", "agents_profile", "activities_profile")


def mint_au_credential(lrs_url: str, admin_user: str, admin_password: str, timeout: float = 15.0) -> str:
    """Create a scoped credential and return it as base64 ``key:secret`` (the Basic value)."""
    base = lrs_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        login = client.post(f"{base}/admin/account/login", json={"username": admin_user, "password": admin_password})
        login.raise_for_status()
        jwt = login.json()["json-web-token"]
        made = client.post(
            f"{base}/admin/creds", json={"scopes": list(AU_SCOPES)}, headers={"Authorization": f"Bearer {jwt}"}
        )
        made.raise_for_status()
        cred = made.json()
    return base64.b64encode(f"{cred['api-key']}:{cred['secret-key']}".encode()).decode()


def main() -> int:  # pragma: no cover - operator CLI
    url, user, password = (os.getenv(k, "") for k in ("LRS_URL", "LRS_ADMIN_USER", "LRS_ADMIN_PASSWORD"))
    if not (url and user and password):
        print("set LRS_URL, LRS_ADMIN_USER and LRS_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    print(f"CMI5_LRS_AUTH={mint_au_credential(url, user, password)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
