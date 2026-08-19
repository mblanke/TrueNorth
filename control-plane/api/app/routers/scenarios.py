"""TrueNorth Range — Scenarios router.

CRUD operations for exercise scenarios (YAML threat definitions).

Permissions required per endpoint:

=========================================  ==========================
Endpoint                                   Permission(s)
=========================================  ==========================
POST   /scenarios                          SCENARIO_CREATE
GET    /scenarios                          SCENARIO_READ
GET    /scenarios/{scenario_id}            SCENARIO_READ
PUT    /scenarios/{scenario_id}            SCENARIO_UPDATE
DELETE /scenarios/{scenario_id}            SCENARIO_DELETE
=========================================  ==========================
"""

from __future__ import annotations

import logging
import uuid

import yaml as pyyaml
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..tenancy import get_owned
from ..db import get_db
from ..models import AuditLog, Scenario, UserRole
from ..rbac import Permission, require_permission
from ..schemas import ScenarioIn, ScenarioListOut, ScenarioOut, ScenarioUpdate

logger = logging.getLogger("truenorth.api.scenarios")

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str) -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid))


@router.post("", response_model=ScenarioOut, status_code=201)
def create_scenario(
    body: ScenarioIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_CREATE)),
) -> Scenario:
    """Create a new scenario.  **Permission: scenario:create**"""
    try:
        parsed = pyyaml.safe_load(body.yaml)
        if not isinstance(parsed, dict):
            raise HTTPException(422, "Scenario YAML must be a mapping")
    except pyyaml.YAMLError as e:
        raise HTTPException(422, f"Invalid YAML: {e}") from e
    sc = Scenario(
        name=body.name,
        version=body.version,
        yaml=body.yaml,
        is_public=body.is_public,
        tenant_id=uuid.UUID(user.tenant_id),
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)
    _audit(db, user, "create", "scenario", str(sc.id))
    db.commit()
    return sc


@router.get("", response_model=list[ScenarioListOut])
def list_scenarios(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_READ)),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[Scenario]:
    """List scenarios visible to user.  **Permission: scenario:read**"""
    q = db.query(Scenario).filter(
        (Scenario.tenant_id == uuid.UUID(user.tenant_id)) | (Scenario.is_public == True)  # noqa: E712
    )
    return q.order_by(Scenario.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/{scenario_id}", response_model=ScenarioOut)
def get_scenario(
    scenario_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_READ)),
) -> Scenario:
    """Retrieve a single scenario.  **Permission: scenario:read**"""
    sc = get_owned(db, Scenario, scenario_id, user, not_found="Scenario not found")
    return sc


@router.put("/{scenario_id}", response_model=ScenarioOut)
def update_scenario(
    scenario_id: uuid.UUID,
    body: ScenarioUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_UPDATE)),
) -> Scenario:
    """Update a scenario.  **Permission: scenario:update**"""
    sc = get_owned(db, Scenario, scenario_id, user, not_found="Scenario not found")
    if sc.tenant_id and sc.tenant_id != uuid.UUID(user.tenant_id) and user.role != UserRole.admin:
        raise HTTPException(403, "Not authorized")
    try:
        parsed = pyyaml.safe_load(body.yaml)
        if not isinstance(parsed, dict):
            raise HTTPException(422, "Scenario YAML must be a mapping")
    except pyyaml.YAMLError as e:
        raise HTTPException(422, f"Invalid YAML: {e}") from e
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(sc, key, value)
    db.commit()
    db.refresh(sc)
    return sc


@router.delete("/{scenario_id}", status_code=204, response_class=Response)
def delete_scenario(
    scenario_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_DELETE)),
):
    """Delete a scenario.  **Permission: scenario:delete**"""
    sc = get_owned(db, Scenario, scenario_id, user, not_found="Scenario not found")
    db.delete(sc)
    db.commit()
