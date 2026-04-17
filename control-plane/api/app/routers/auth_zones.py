"""TrueNorth Range -- Auth Zones Router.

Zone-based authentication policies (FIDO2, Kerberos, session tokens).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuthZonePolicy
from ..schemas import AuthZonePolicyIn, AuthZonePolicyOut

router = APIRouter(prefix="/auth-zones", tags=["Directory"])


@router.get("", response_model=list[AuthZonePolicyOut])
def list_zones(db: Session = Depends(get_db)):
    return db.query(AuthZonePolicy).order_by(AuthZonePolicy.zone_name).all()


@router.post("", response_model=AuthZonePolicyOut, status_code=201)
def create_zone(payload: AuthZonePolicyIn, db: Session = Depends(get_db)):
    zone = AuthZonePolicy(
        zone_name=payload.zone_name,
        description=payload.description,
        allowed_methods=payload.allowed_methods,
        require_mfa=payload.require_mfa,
        session_timeout_minutes=payload.session_timeout_minutes,
        max_failed_attempts=payload.max_failed_attempts,
        ip_whitelist=payload.ip_whitelist,
        clearance_required=payload.clearance_required,
    )
    db.add(zone)
    db.commit()
    db.refresh(zone)
    return zone


@router.get("/{zone_id}", response_model=AuthZonePolicyOut)
def get_zone(zone_id: uuid.UUID, db: Session = Depends(get_db)):
    zone = db.get(AuthZonePolicy, str(zone_id))
    if not zone:
        raise HTTPException(404, "Zone not found")
    return zone


@router.patch("/{zone_id}", response_model=AuthZonePolicyOut)
def update_zone(zone_id: uuid.UUID, payload: AuthZonePolicyIn, db: Session = Depends(get_db)):
    zone = db.get(AuthZonePolicy, str(zone_id))
    if not zone:
        raise HTTPException(404, "Zone not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(zone, field, value)
    db.commit()
    db.refresh(zone)
    return zone


@router.delete("/{zone_id}", status_code=204, response_class=Response)
def delete_zone(zone_id: uuid.UUID, db: Session = Depends(get_db)):
    zone = db.get(AuthZonePolicy, str(zone_id))
    if not zone:
        raise HTTPException(404, "Zone not found")
    db.delete(zone)
    db.commit()
