"""Storage appliance & volume CRUD."""
from __future__ import annotations
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import get_current_user, CurrentUser
from ..models import StorageAppliance, StorageVolume
from ..schemas import (
    StorageApplianceIn, StorageApplianceOut,
    StorageVolumeIn, StorageVolumeOut,
    StorageSummaryOut,
)

router = APIRouter(prefix="/storage", tags=["storage"])


# ── Appliances ─────────────────────────────────────────────────
@router.get("/appliances", response_model=list[StorageApplianceOut])
def list_appliances(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return db.query(StorageAppliance).filter(StorageAppliance.tenant_id == user.tenant_id).all()


@router.post("/appliances", response_model=StorageApplianceOut, status_code=201)
def create_appliance(body: StorageApplianceIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = StorageAppliance(**body.model_dump(), tenant_id=user.tenant_id)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/appliances/{appliance_id}", status_code=204)
def delete_appliance(appliance_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = db.query(StorageAppliance).filter_by(id=appliance_id, tenant_id=user.tenant_id).first()
    if not obj:
        raise HTTPException(404, "Appliance not found")
    db.delete(obj)
    db.commit()


# ── Volumes ────────────────────────────────────────────────────
@router.get("/volumes", response_model=list[StorageVolumeOut])
def list_volumes(appliance_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    q = db.query(StorageVolume).filter(StorageVolume.tenant_id == user.tenant_id)
    if appliance_id:
        q = q.filter(StorageVolume.appliance_id == appliance_id)
    return q.all()


@router.post("/volumes", response_model=StorageVolumeOut, status_code=201)
def create_volume(body: StorageVolumeIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = StorageVolume(**body.model_dump(), tenant_id=user.tenant_id)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/volumes/{volume_id}", status_code=204)
def delete_volume(volume_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = db.query(StorageVolume).filter_by(id=volume_id, tenant_id=user.tenant_id).first()
    if not obj:
        raise HTTPException(404, "Volume not found")
    db.delete(obj)
    db.commit()


# ── Summary ────────────────────────────────────────────────────
@router.get("/summary", response_model=StorageSummaryOut)
def storage_summary(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    appliances = db.query(StorageAppliance).filter(StorageAppliance.tenant_id == user.tenant_id).all()
    volumes = db.query(StorageVolume).filter(StorageVolume.tenant_id == user.tenant_id).all()
    return StorageSummaryOut(
        total_appliances=len(appliances),
        active_appliances=sum(1 for a in appliances if a.is_active),
        total_raw_tb=sum(a.raw_capacity_tb for a in appliances),
        total_usable_tb=sum(a.usable_capacity_tb for a in appliances),
        total_volumes=len(volumes),
    )
