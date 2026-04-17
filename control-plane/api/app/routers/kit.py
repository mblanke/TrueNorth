"""Kit definition CRUD."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import KitDefinition
from ..schemas import KitDefinitionIn, KitDefinitionOut

router = APIRouter(prefix="/kits", tags=["kits"])


@router.get("/", response_model=list[KitDefinitionOut])
def list_kits(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return db.query(KitDefinition).filter(KitDefinition.tenant_id == user.tenant_id).all()


@router.post("/", response_model=KitDefinitionOut, status_code=201)
def create_kit(body: KitDefinitionIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = KitDefinition(**body.model_dump(), tenant_id=user.tenant_id)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{kit_id}", status_code=204, response_class=Response)
def delete_kit(kit_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    obj = db.query(KitDefinition).filter_by(id=kit_id, tenant_id=user.tenant_id).first()
    if not obj:
        raise HTTPException(404, "Kit not found")
    db.delete(obj)
    db.commit()
