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
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import engine_bridge
from ..auth import CurrentUser
from ..db import get_db
from ..detections.redaction import redact_scenario_yaml, sees_answer_key
from ..models import AuditLog, Exercise, Scenario, UserRole
from ..rbac import Permission, require_permission
from ..scheduler import service as scheduler
from ..schemas import ScenarioIn, ScenarioListOut, ScenarioOut, ScenarioUpdate
from ..tenancy import get_owned

logger = logging.getLogger("truenorth.api.scenarios")

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class YamlValidateIn(BaseModel):
    yaml: str


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str) -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            tenant_id=uuid.UUID(user.tenant_id),
            action=action,
            resource_type=rtype,
            resource_id=rid,
        )
    )


@router.post("/validate")
def validate_scenario(
    body: YamlValidateIn,
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_READ)),
) -> dict:
    """Validate scenario YAML against the engine's canonical schema.

    Returns ``{valid, errors: [{path, message}], normalized}`` — ``normalized``
    is the parsed document whenever the YAML parses, even when schema-invalid,
    so the Scenario Studio can load a document and show its problems at once.
    Create/update stay permissive on purpose (forge output and older seeds are
    not schema-clean); this endpoint is the contract for authored content.
    """
    return engine_bridge.validate_yaml("scenario", body.yaml)


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
    """Retrieve a single scenario.  **Permission: scenario:read**

    Without scenario:update the YAML is the briefing only: no objective params,
    variables or inject playbook (ADR 0005 §5).
    """
    sc = get_owned(db, Scenario, scenario_id, user, not_found="Scenario not found")
    if sees_answer_key(user):
        return sc
    return ScenarioOut.model_validate(sc).model_copy(update={"yaml": redact_scenario_yaml(sc.yaml)})


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
    update_data = body.model_dump(exclude_unset=True)
    if "yaml" in update_data:  # a rename or visibility change carries no yaml
        try:
            parsed = pyyaml.safe_load(body.yaml)
            if not isinstance(parsed, dict):
                raise HTTPException(422, "Scenario YAML must be a mapping")
        except pyyaml.YAMLError as e:
            raise HTTPException(422, f"Invalid YAML: {e}") from e
    for key, value in update_data.items():
        setattr(sc, key, value)
    db.commit()
    db.refresh(sc)
    return sc


@router.delete(
    "/{scenario_id}",
    status_code=204,
    response_class=Response,
    responses={409: {"description": "An exercise references the scenario"}},
)
def delete_scenario(
    scenario_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_DELETE)),
):
    """Delete a scenario.  **Permission: scenario:delete**

    409 while any exercise (including a finished or soft-deleted one, whose AAR still
    reads the scenario) references it. Checked up front (SQLite does not enforce the FK)
    and again at commit, for an exercise created in between. Also 409 while a scheduled
    event not yet completed or cancelled will run it; finished events keep their row
    without the scenario. Scenario executions do not block: they keep their record and
    lose the link.
    """
    sc = get_owned(db, Scenario, scenario_id, user, not_found="Scenario not found")
    in_use = db.query(func.count(Exercise.id)).filter(Exercise.scenario_id == sc.id).scalar() or 0
    if in_use:
        raise HTTPException(409, _in_use_message(in_use))
    if booked := scheduler.reserving_events_for(db, "scenario", sc.id):
        raise HTTPException(409, f"Scenario is booked by {booked} scheduled event(s); cancel them first")
    scheduler.detach(db, "scenario", sc.id)
    db.delete(sc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, _in_use_message(None)) from None


def _in_use_message(count: int | None) -> str:
    used = f"{count} exercise(s)" if count else "an exercise"
    return f"Scenario is used by {used}; delete those exercises or point them at another scenario first"
