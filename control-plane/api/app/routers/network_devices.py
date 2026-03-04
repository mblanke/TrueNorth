"""Network device CRUD."""
from __future__ import annotations
import uuid
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import get_current_user, CurrentUser
from ..models import NetworkDevice
from ..schemas import NetworkDeviceIn, NetworkDeviceOut, NetworkSummaryOut

router = APIRouter(prefix="/network-devices", tags=["network"])


@router.get("/", response_model=list[NetworkDeviceOut])
def list_devices(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return db.query(NetworkDevice).filter(NetworkDevice.tenant_id == user.tenant_id).all()


@router.post("/", response_model=NetworkDeviceOut, status_code=201)
def create_device(body: NetworkDeviceIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = NetworkDevice(**body.model_dump(), tenant_id=user.tenant_id)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = db.query(NetworkDevice).filter_by(id=device_id, tenant_id=user.tenant_id).first()
    if not obj:
        raise HTTPException(404, "Device not found")
    db.delete(obj)
    db.commit()


@router.get("/summary", response_model=NetworkSummaryOut)
def network_summary(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    devices = db.query(NetworkDevice).filter(NetworkDevice.tenant_id == user.tenant_id).all()
    by_role = dict(Counter(d.role.value if hasattr(d.role, 'value') else d.role for d in devices))
    return NetworkSummaryOut(
        total_devices=len(devices),
        active_devices=sum(1 for d in devices if d.is_active),
        by_role=by_role,
    )
