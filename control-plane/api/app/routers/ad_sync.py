"""TrueNorth Range -- AD/LDAP Synchronization Router.

Provides status, trigger, and config endpoints for Active Directory integration.

Keycloak owns the AD→Keycloak half of synchronisation: it federates the
directory over LDAPS and imports users on a schedule. Keycloak→TrueNorth
happens at registration, when an approved account is created. So these
endpoints do not talk LDAP themselves — they drive Keycloak's federation
component and report what it did.

The component id comes from ``KEYCLOAK_LDAP_COMPONENT_ID``, which the installer
writes after creating the federation provider (install/roles/tn_keycloak).
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser, require_role
from ..db import get_db
from ..models import SecurityGroup, User, UserRole
from ..rbac import Permission, require_permission

logger = logging.getLogger("truenorth.api.ad_sync")

router = APIRouter(prefix="/ad-sync", tags=["AD Sync"])

# Sources that indicate an account whose identity came from the directory:
# "ad" is written by registration approval, "ad_sync" by a bulk import.
AD_SOURCES = ("ad", "ad_sync")


def _get_ldap_config() -> dict:
    """Read LDAP/AD config from environment variables with safe defaults."""
    return {
        "server": os.getenv("LDAP_SERVER", ""),
        "port": int(os.getenv("LDAP_PORT", "636")),
        "use_ssl": os.getenv("LDAP_USE_SSL", "true").lower() == "true",
        "bind_dn": os.getenv("LDAP_BIND_DN", ""),
        "base_dn": os.getenv("LDAP_BASE_DN", ""),
        "user_search_base": os.getenv("LDAP_USER_SEARCH_BASE", ""),
        "group_search_base": os.getenv("LDAP_GROUP_SEARCH_BASE", ""),
        "configured": bool(os.getenv("LDAP_SERVER")),
    }


def _keycloak_config() -> dict:
    base = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    if base and not base.endswith("/auth"):
        base = f"{base}/auth"
    return {
        "base": base,
        "realm": os.getenv("KEYCLOAK_REALM", "truenorth"),
        "component_id": os.getenv("KEYCLOAK_LDAP_COMPONENT_ID", ""),
        "admin_user": os.getenv("KEYCLOAK_ADMIN_USER", ""),
        "admin_password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", ""),
    }


def _admin_token(cfg: dict) -> str:
    resp = httpx.post(
        f"{cfg['base']}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": cfg["admin_user"],
            "password": cfg["admin_password"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@router.get("/status")
def ad_sync_status(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_READ)),
):
    """Return current AD sync status and statistics.  **Permission: user:read**"""
    ad_users = db.query(User).filter(User.source.in_(AD_SOURCES)).count()
    ad_groups = db.query(SecurityGroup).filter(SecurityGroup.ad_object_guid.isnot(None)).count()
    last_sync = (
        db.query(func.max(User.last_synced_at)).filter(User.source.in_(AD_SOURCES)).scalar()
    )
    config = _get_ldap_config()
    kc = _keycloak_config()

    # Ask Keycloak when the federation provider last ran, rather than inferring
    # it from our own rows — those only update when someone is approved.
    federation_last_sync = None
    federation_error = None
    if kc["base"] and kc["component_id"] and kc["admin_password"]:
        try:
            token = _admin_token(kc)
            resp = httpx.get(
                f"{kc['base']}/admin/realms/{kc['realm']}/components/{kc['component_id']}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            cfg = resp.json().get("config", {})
            federation_last_sync = (cfg.get("lastSync") or [None])[0]
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not raised
            federation_error = str(exc)
            logger.warning("Could not read Keycloak federation state: %s", exc)

    return {
        "connected": config["configured"],
        "users_synced": ad_users,
        "groups_synced": ad_groups,
        "last_sync_at": last_sync.isoformat() if last_sync else None,
        "server": config["server"] if config["configured"] else None,
        "federation_configured": bool(kc["component_id"]),
        "federation_last_sync": federation_last_sync,
        "federation_error": federation_error,
    }


@router.post("/trigger")
def trigger_ad_sync(
    full: bool = True,
    user: CurrentUser = Depends(require_role(UserRole.admin)),
):
    """Trigger a Keycloak user-federation sync.  **Role: admin**

    This previously returned a fabricated task id and ``status: "triggered"``
    without contacting anything, so a broken federation looked identical to a
    working one. It now calls Keycloak and reports what Keycloak said.
    """
    config = _get_ldap_config()
    kc = _keycloak_config()

    if not config["configured"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "AD/LDAP is not configured. Set LDAP_SERVER, LDAP_BIND_DN and LDAP_BASE_DN.",
        )

    if not (kc["base"] and kc["component_id"]):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Keycloak user federation is not configured. KEYCLOAK_LDAP_COMPONENT_ID is "
            "set by the installer (install/roles/tn_keycloak) once the federation "
            "provider exists.",
        )

    action = "triggerFullSync" if full else "triggerChangedUsersSync"
    try:
        token = _admin_token(kc)
        resp = httpx.post(
            f"{kc['base']}/admin/realms/{kc['realm']}/user-storage/"
            f"{kc['component_id']}/sync",
            params={"action": action},
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("Keycloak sync rejected: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Keycloak rejected the sync request: {exc.response.status_code}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Keycloak sync failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Keycloak: {exc}"
        ) from exc

    result = resp.json() if resp.content else {}
    logger.info("Keycloak federation sync (%s) result: %s", action, result)
    return {
        "status": "completed",
        "action": action,
        "added": result.get("added", 0),
        "updated": result.get("updated", 0),
        "removed": result.get("removed", 0),
        "failed": result.get("failed", 0),
        "message": (
            "Directory users are now in Keycloak. TrueNorth accounts are still "
            "created by approving a registration request."
        ),
    }


@router.get("/config")
def ad_sync_config(
    user: CurrentUser = Depends(require_role(UserRole.admin)),
):
    """Return current AD/LDAP configuration.  **Role: admin**

    Deliberately admin-only: this describes the directory topology (server,
    base DN, search bases) and was previously readable by anyone at all.
    """
    config = _get_ldap_config()
    kc = _keycloak_config()
    bind = config["bind_dn"]
    return {
        "server": config["server"] or "(not set)",
        "port": config["port"],
        "use_ssl": config["use_ssl"],
        "bind_dn": (bind[:20] + "...") if len(bind) > 20 else (bind or "(not set)"),
        "base_dn": config["base_dn"] or "(not set)",
        "user_search_base": config["user_search_base"] or "(not set)",
        "group_search_base": config["group_search_base"] or "(not set)",
        "configured": config["configured"],
        "keycloak_realm": kc["realm"],
        "keycloak_component_id": kc["component_id"] or "(not set)",
    }
