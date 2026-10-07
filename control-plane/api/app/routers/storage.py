"""Storage appliance & volume CRUD.

Infrastructure inventory, so it follows ``hypervisors.py``: reading needs
``infra:read``, creating, changing or deleting needs ``infra:write``. Until
2026-10-07 every route here needed only a login, so a student could register or
delete a storage appliance. Rows are tenant-owned; a foreign id is 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..delete_guard import commit_delete, refuse_if
from ..models import StorageAppliance, StorageVolume
from ..rbac import Permission, require_permission
from ..schemas import (
    StorageApplianceIn,
    StorageApplianceOut,
    StorageApplianceUpdate,
    StorageSummaryOut,
    StorageVolumeIn,
    StorageVolumeOut,
)
from ..tenancy import get_owned, tenant_uuid

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_permission(Permission.INFRA_READ))])
WRITE = [Depends(require_permission(Permission.INFRA_WRITE))]


# ── Appliances ─────────────────────────────────────────────────
@router.get("/appliances", response_model=list[StorageApplianceOut])
def list_appliances(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return db.query(StorageAppliance).filter(StorageAppliance.tenant_id == tenant_uuid(user)).all()


@router.post("/appliances", response_model=StorageApplianceOut, status_code=201, dependencies=WRITE)
def create_appliance(
    body: StorageApplianceIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    obj = StorageAppliance(**body.model_dump(), tenant_id=tenant_uuid(user))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/appliances/{appliance_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_appliance(
    appliance_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """Delete an appliance.  409 while volumes are still recorded on it."""
    obj = get_owned(db, StorageAppliance, appliance_id, user, not_found="Appliance not found")
    # tenant-safe: obj came from get_owned(); a count discloses no row.
    volumes = db.query(StorageVolume.id).filter(StorageVolume.appliance_id == obj.id)
    refuse_if(volumes, "Appliance has {n} volume(s); delete them first")
    db.delete(obj)
    commit_delete(db, "Appliance")


@router.patch("/appliances/{appliance_id}", response_model=StorageApplianceOut, dependencies=WRITE)
def update_appliance(
    appliance_id: uuid.UUID,
    body: StorageApplianceUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    obj = get_owned(db, StorageAppliance, appliance_id, user, not_found="Appliance not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    db.commit()
    db.refresh(obj)
    return obj


# ── Volumes ────────────────────────────────────────────────────
@router.get("/volumes", response_model=list[StorageVolumeOut])
def list_volumes(
    appliance_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    q = db.query(StorageVolume).filter(StorageVolume.tenant_id == tenant_uuid(user))
    if appliance_id:
        q = q.filter(StorageVolume.appliance_id == appliance_id)
    return q.all()


@router.post("/volumes", response_model=StorageVolumeOut, status_code=201, dependencies=WRITE)
def create_volume(body: StorageVolumeIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    # A volume on another tenant's appliance would carve space out of their storage.
    get_owned(db, StorageAppliance, body.appliance_id, user, not_found="Appliance not found")
    obj = StorageVolume(**body.model_dump(), tenant_id=tenant_uuid(user))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/volumes/{volume_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_volume(volume_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = get_owned(db, StorageVolume, volume_id, user, not_found="Volume not found")
    db.delete(obj)
    db.commit()


# ── Summary ────────────────────────────────────────────────────
@router.get("/summary", response_model=StorageSummaryOut)
def storage_summary(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    tid = tenant_uuid(user)
    appliances = db.query(StorageAppliance).filter(StorageAppliance.tenant_id == tid).all()
    volumes = db.query(StorageVolume).filter(StorageVolume.tenant_id == tid).all()
    return StorageSummaryOut(
        total_appliances=len(appliances),
        active_appliances=sum(1 for a in appliances if a.is_active),
        total_raw_tb=sum(a.raw_capacity_tb for a in appliances),
        total_usable_tb=sum(a.usable_capacity_tb for a in appliances),
        total_volumes=len(volumes),
    )
