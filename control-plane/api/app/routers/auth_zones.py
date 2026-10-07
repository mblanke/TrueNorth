"""TrueNorth Range -- Auth Zones Router.

Zone-based authentication policies (FIDO2, Kerberos, session tokens).

Tenancy: a zone with a ``tenant_id`` belongs to that tenant; a zone with a NULL
``tenant_id`` is a platform default (``seed.seed_auth_zones``) that every tenant sees.
Callers see their own zones plus the defaults, and a foreign tenant's zone is 404.
Changing a platform default changes it for every tenant, so that also needs
``tenant:update`` (platform-level), not only the router's ``user:update``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import AuthZonePolicy
from ..rbac import Permission, require_permission, user_has_permission
from ..schemas import AuthZonePolicyIn, AuthZonePolicyOut
from ..tenancy import get_owned_or_global, tenant_uuid

# Router-level authentication. Authentication zone policy — MFA requirements, clearance floors and IP
# allow-lists. Changing these changes who can get in.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/auth-zones", tags=["Directory"], dependencies=[Depends(require_permission(Permission.USER_UPDATE))])


def _writable(db: Session, zone_id: uuid.UUID, user: CurrentUser) -> AuthZonePolicy:
    zone = get_owned_or_global(db, AuthZonePolicy, zone_id, user, not_found="Zone not found")
    if zone.tenant_id is None and not user_has_permission(user, Permission.TENANT_UPDATE):
        raise HTTPException(403, "Platform default zones need tenant:update to change")
    return zone


def _commit_unique(db: Session) -> None:
    # zone_name is unique platform-wide (a schema constraint, not a tenant one).
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "A zone with that name already exists") from exc


@router.get("", response_model=list[AuthZonePolicyOut])
def list_zones(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return (
        db.query(AuthZonePolicy)
        .filter((AuthZonePolicy.tenant_id == tenant_uuid(user)) | AuthZonePolicy.tenant_id.is_(None))
        .order_by(AuthZonePolicy.zone_name)
        .all()
    )


@router.post("", response_model=AuthZonePolicyOut, status_code=201)
def create_zone(payload: AuthZonePolicyIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    zone = AuthZonePolicy(
        zone_name=payload.zone_name,
        description=payload.description,
        allowed_methods=payload.allowed_methods,
        require_mfa=payload.require_mfa,
        session_timeout_minutes=payload.session_timeout_minutes,
        max_failed_attempts=payload.max_failed_attempts,
        ip_whitelist=payload.ip_whitelist,
        clearance_required=payload.clearance_required,
        tenant_id=tenant_uuid(user),
    )
    db.add(zone)
    _commit_unique(db)
    db.refresh(zone)
    return zone


@router.get("/{zone_id}", response_model=AuthZonePolicyOut)
def get_zone(zone_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return get_owned_or_global(db, AuthZonePolicy, zone_id, user, not_found="Zone not found")


@router.patch("/{zone_id}", response_model=AuthZonePolicyOut)
def update_zone(
    zone_id: uuid.UUID,
    payload: AuthZonePolicyIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    zone = _writable(db, zone_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(zone, field, value)
    _commit_unique(db)
    db.refresh(zone)
    return zone


@router.delete("/{zone_id}", status_code=204, response_class=Response)
def delete_zone(zone_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    zone = _writable(db, zone_id, user)
    db.delete(zone)
    db.commit()
