"""Point the shipped demo people's roster rows at their Keycloak accounts (staging/demo only).

scripts/load_content.py's demo layer creates the demo people as roster rows with a
placeholder Keycloak subject, so nobody can sign in as them. On a host without AD, with
tn_load_shipped_demo on, the installer gives each a local Keycloak account
(install/roles/tn_keycloak/tasks/demo_accounts.yml) and then runs this, as it runs
app.bootstrap_admin for the administrator:

    docker compose run --rm --no-deps --entrypoint "" \\
      -e TN_DEMO_EMAILS=instructor.demo@truenorth.test,trainee1.demo@truenorth.test \\
      -e KEYCLOAK_ADMIN_USER -e KEYCLOAK_ADMIN_PASSWORD \\
      api python -m app.link_demo_accounts

Each row's keycloak_id becomes its account's subject; nothing else changes (the role is
the loader's). Only demo addresses (``*.demo@truenorth.test``) are touched, so a mistyped
list cannot re-point a real person's row. Idempotent: a row already linked is left alone.
"""

from __future__ import annotations

import logging
import os
import sys

import httpx
from sqlalchemy.orm import Session

from .bootstrap_admin import _keycloak_admin_token
from .db import SessionLocal
from .models import User

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("truenorth.demo_accounts")

DEMO_DOMAIN_SUFFIX = ".demo@truenorth.test"


def is_demo_email(email: str) -> bool:
    return email.lower().endswith(DEMO_DOMAIN_SUFFIX) and len(email) > len(DEMO_DOMAIN_SUFFIX)


def find_keycloak_user(base: str, realm: str, token: str, email: str) -> dict | None:
    """The realm's account for ``email``, by exact username then exact email (no fallback)."""
    headers = {"Authorization": f"Bearer {token}"}
    for params in ({"username": email, "exact": "true"}, {"email": email, "exact": "true"}):
        resp = httpx.get(f"{base}/admin/realms/{realm}/users", params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return rows[0]
    return None


def link(db: Session, email: str, sub: str) -> str:
    """Point the roster row for ``email`` at Keycloak subject ``sub``.

    Returns "linked", "unchanged", "no roster row" or "subject in use".
    """
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        return "no roster row"
    if user.keycloak_id == sub:
        return "unchanged"
    other = db.query(User).filter(User.keycloak_id == sub, User.id != user.id).first()
    if other is not None:
        return "subject in use"
    user.keycloak_id = sub
    db.flush()
    return "linked"


def main() -> int:
    emails = [e.strip() for e in os.getenv("TN_DEMO_EMAILS", "").split(",") if e.strip()]
    if not emails:
        logger.error("TN_DEMO_EMAILS is not set — nothing to do.")
        return 2
    # Logs name a person by position in TN_DEMO_EMAILS, never by address (test_no_pii_in_logs).
    not_demo = [n for n, e in enumerate(emails, 1) if not is_demo_email(e)]
    if not_demo:
        logger.error("refusing non-demo addresses (only *%s) at positions %s", DEMO_DOMAIN_SUFFIX, not_demo)
        return 2

    kc_base = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    if not kc_base.endswith("/auth"):
        kc_base = f"{kc_base}/auth"
    realm = os.getenv("KEYCLOAK_REALM", "truenorth")
    try:
        token = _keycloak_admin_token(
            kc_base, os.getenv("KEYCLOAK_ADMIN_USER", "admin"), os.getenv("KEYCLOAK_ADMIN_PASSWORD", "")
        )
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        logger.error("could not authenticate to Keycloak at %s: %s", kc_base, exc)
        return 3

    failed = False
    db = SessionLocal()
    try:
        for n, email in enumerate(emails, 1):
            who = f"demo person {n}/{len(emails)}"
            try:
                found = find_keycloak_user(kc_base, realm, token, email)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s: Keycloak lookup failed: %s", who, exc)
                failed = True
                continue
            if found is None:
                logger.error("%s: no Keycloak account in realm %s (60-keycloak creates it)", who, realm)
                failed = True
                continue
            result = link(db, email, found["id"])
            failed = failed or result not in ("linked", "unchanged")
            logger.info("%s %s (sub %s)", result, who, found["id"])
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("linking the demo accounts failed: %s", exc)
        return 5
    finally:
        db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
