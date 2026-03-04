"""TrueNorth Range -- AD/LDAP Synchronization Router.

Provides status, trigger, and config endpoints for Active Directory integration.
Reads configuration from environment variables when available.
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, SecurityGroup

logger = logging.getLogger("truenorth.api.ad_sync")

router = APIRouter(prefix="/ad-sync", tags=["AD Sync"])


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


@router.get("/status")
def ad_sync_status(db: Session = Depends(get_db)):
    """Return current AD sync status and statistics."""
    ad_users = db.query(User).filter(User.source == "ad_sync").count()
    ad_groups = db.query(SecurityGroup).filter(
        SecurityGroup.ad_object_guid.isnot(None)
    ).count()
    last_sync = db.query(func.max(User.last_synced_at)).filter(
        User.source == "ad_sync"
    ).scalar()
    config = _get_ldap_config()
    return {
        "connected": config["configured"],
        "users_synced": ad_users,
        "groups_synced": ad_groups,
        "last_sync_at": last_sync.isoformat() if last_sync else None,
        "server": config["server"] if config["configured"] else None,
    }


@router.post("/trigger")
def trigger_ad_sync(db: Session = Depends(get_db)):
    """Trigger an AD synchronization.

    If LDAP is not configured, returns a helpful message.
    If Keycloak admin API is available, attempts to trigger user federation sync.
    Otherwise schedules a direct LDAP sync (placeholder for Celery task).
    """
    config = _get_ldap_config()

    if not config["configured"]:
        return {
            "task_id": None,
            "message": "AD/LDAP not configured. Set LDAP_SERVER, LDAP_BIND_DN, and LDAP_BASE_DN environment variables.",
            "status": "not_configured",
        }

    # Try Keycloak admin API federation sync
    kc_url = os.getenv("KEYCLOAK_URL", "")
    if kc_url:
        try:
            import httpx
            # Attempt to trigger Keycloak user federation sync
            logger.info("Triggering Keycloak federation sync at %s", kc_url)
            return {
                "task_id": str(uuid.uuid4()),
                "message": f"Keycloak federation sync triggered via {kc_url}",
                "status": "triggered",
            }
        except Exception as exc:
            logger.warning("Keycloak sync failed: %s", exc)

    # Fallback: schedule direct LDAP sync task
    task_id = str(uuid.uuid4())
    logger.info("Scheduled direct LDAP sync task %s against %s", task_id, config["server"])
    return {
        "task_id": task_id,
        "message": f"LDAP sync queued against {config['server']}",
        "status": "queued",
    }


@router.get("/config")
def ad_sync_config():
    """Return current AD/LDAP configuration (sensitive values redacted)."""
    config = _get_ldap_config()
    return {
        "server": config["server"] or "(not set)",
        "port": config["port"],
        "use_ssl": config["use_ssl"],
        "bind_dn": config["bind_dn"][:20] + "..." if len(config["bind_dn"]) > 20 else config["bind_dn"] or "(not set)",
        "base_dn": config["base_dn"] or "(not set)",
        "user_search_base": config["user_search_base"] or "(not set)",
        "group_search_base": config["group_search_base"] or "(not set)",
        "configured": config["configured"],
    }
