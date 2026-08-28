"""Create the tenant and the first administrator, for a production install.

Registration is the only path that creates a trainee, and it requires an
approver — so a fresh deployment needs exactly one account to exist before
anybody can log in usefully. In development that gap is filled by
``_seed_dev_data``, which mints a hardcoded ``admin@truenorth.local`` with a
well-known UUID. That account must not exist in production, so ``SEED_DEV_DATA``
is false there and this module fills the gap instead.

The administrator is a *named Active Directory account*, resolved to its
Keycloak subject through the Admin API. There is no local password: the person
signs in with their own directory credentials like everyone else, and the audit
trail names a human rather than "admin".

Run as a one-shot container:

    docker compose run --rm --no-deps --entrypoint "" \\
      -e TN_TENANT_NAME="TrueNorth Range" \\
      -e TN_TENANT_SLUG=default \\
      -e TN_BOOTSTRAP_ADMIN_UPN=maj.smith@corp.tnrange.lab \\
      api python -m app.bootstrap_admin

Idempotent: re-running promotes/repairs the same account rather than creating
a second one.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

import httpx
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Tenant, User, UserRole

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("truenorth.bootstrap")


def _keycloak_admin_token(base: str, user: str, password: str) -> str:
    resp = httpx.post(
        f"{base}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": user,
            "password": password,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _find_keycloak_user(base: str, realm: str, token: str, upn: str) -> dict | None:
    """Look the UPN up in the realm, by username then by email."""
    headers = {"Authorization": f"Bearer {token}"}
    for params in ({"username": upn, "exact": "true"}, {"email": upn, "exact": "true"}):
        resp = httpx.get(
            f"{base}/admin/realms/{realm}/users",
            params=params,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return rows[0]

    # AD usernames are often the sAMAccountName rather than the full UPN.
    local = upn.split("@", 1)[0]
    resp = httpx.get(
        f"{base}/admin/realms/{realm}/users",
        params={"username": local, "exact": "true"},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def ensure_tenant(db: Session, name: str, slug: str) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if tenant is not None:
        return tenant
    tenant = Tenant(id=uuid.uuid4(), name=name, slug=slug)
    db.add(tenant)
    db.flush()
    logger.info("created tenant %s (%s)", name, slug)
    return tenant


def ensure_admin(db: Session, tenant: Tenant, kc_user: dict) -> User:
    """Create or repair the bootstrap administrator."""
    sub = kc_user["id"]
    email = (kc_user.get("email") or "").strip()
    display = (
        f"{kc_user.get('firstName', '')} {kc_user.get('lastName', '')}".strip()
        or kc_user.get("username")
        or email
    )

    user = db.query(User).filter(User.keycloak_id == sub).first()
    if user is None and email:
        # Adopt a row created by a CSV roster import rather than colliding on
        # the unique email constraint.
        user = db.query(User).filter(User.email == email).first()

    if user is None:
        user = User(
            id=uuid.uuid4(),
            keycloak_id=sub,
            email=email or f"{kc_user.get('username')}@invalid",
            display_name=display,
        )
        db.add(user)
        action = "created"
    else:
        action = "updated"

    user.keycloak_id = sub
    user.display_name = display or user.display_name
    user.role = UserRole.admin
    user.tenant_id = tenant.id
    user.source = "ad"
    user.is_active = True
    # An administrator is not a trainee; do not trap them in first-run.
    user.onboarding_state = "complete"
    db.flush()
    logger.info("%s bootstrap admin %s (%s)", action, display, email or sub)
    return user


def main() -> int:
    tenant_name = os.getenv("TN_TENANT_NAME", "TrueNorth Range")
    tenant_slug = os.getenv("TN_TENANT_SLUG", "default")
    upn = os.getenv("TN_BOOTSTRAP_ADMIN_UPN", "").strip()

    if not upn:
        logger.error("TN_BOOTSTRAP_ADMIN_UPN is not set — nothing to do.")
        return 2

    kc_base = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    if not kc_base.endswith("/auth"):
        kc_base = f"{kc_base}/auth"
    realm = os.getenv("KEYCLOAK_REALM", "truenorth")
    kc_user = os.getenv("KEYCLOAK_ADMIN_USER", "admin")
    kc_pass = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "")

    try:
        token = _keycloak_admin_token(kc_base, kc_user, kc_pass)
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        logger.error("could not authenticate to Keycloak at %s: %s", kc_base, exc)
        return 3

    try:
        found = _find_keycloak_user(kc_base, realm, token, upn)
    except Exception as exc:  # noqa: BLE001
        logger.error("Keycloak user lookup failed: %s", exc)
        return 3

    if found is None:
        logger.error(
            "No user '%s' in realm '%s'. AD federation imports users on sync — "
            "check the account exists under the configured users DN and that "
            "the sync has run.",
            upn,
            realm,
        )
        return 4

    db = SessionLocal()
    try:
        tenant = ensure_tenant(db, tenant_name, tenant_slug)
        ensure_admin(db, tenant, found)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("bootstrap failed: %s", exc)
        return 5
    finally:
        db.close()

    logger.info("bootstrap complete — %s can now approve registrations", upn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
