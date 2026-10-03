"""TrueNorth Range — Sigma-compatible detection rule editor router."""

from __future__ import annotations

import json
import logging
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db, not_deleted
from ..models import AuditLog, DetectionRule
from ..rbac import Permission, require_permission
from ..schemas import (
    DetectionRuleIn,
    DetectionRuleOut,
    DetectionRuleUpdate,
    SigmaValidationResult,
)

router = APIRouter(prefix="/detection-rules", tags=["detection-rules"])
logger = logging.getLogger("truenorth.api.detection_rules")

_REQUIRED_SIGMA_KEYS = {"title", "logsource", "detection"}
_VALID_LEVELS = {"informational", "low", "medium", "high", "critical"}


def _validate_sigma_yaml(raw_yaml: str) -> SigmaValidationResult:
    """Validate Sigma detection rule YAML structure."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        doc = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        return SigmaValidationResult(valid=False, errors=[f"Invalid YAML: {exc}"])

    if not isinstance(doc, dict):
        return SigmaValidationResult(valid=False, errors=["Root must be a YAML mapping"])

    for key in _REQUIRED_SIGMA_KEYS:
        if key not in doc:
            errors.append(f"Missing required field: '{key}'")

    if "detection" in doc:
        detection = doc["detection"]
        if not isinstance(detection, dict):
            errors.append("'detection' must be a mapping")
        elif "condition" not in detection:
            errors.append("'detection' must include a 'condition' field")

    if "logsource" in doc:
        logsource = doc["logsource"]
        if not isinstance(logsource, dict):
            errors.append("'logsource' must be a mapping")
        elif not any(k in logsource for k in ("category", "product", "service")):
            warnings.append("'logsource' should have at least one of: category, product, service")

    level = doc.get("level")
    if level and level not in _VALID_LEVELS:
        warnings.append(f"Non-standard level '{level}'; expected one of: {', '.join(_VALID_LEVELS)}")

    return SigmaValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


# ── Validation ─────────────────────────────────────────────────────────


@router.post("/validate", response_model=SigmaValidationResult)
async def validate_sigma_rule(
    payload: dict,
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> SigmaValidationResult:
    raw_yaml = payload.get("yaml", "")
    if not raw_yaml:
        return SigmaValidationResult(valid=False, errors=["No YAML content provided"])
    return _validate_sigma_yaml(raw_yaml)


# ── CRUD ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[DetectionRuleOut])
async def list_rules(
    rule_status: str | None = Query(None, alias="status"),
    level: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[DetectionRuleOut]:
    query = db.query(DetectionRule).filter(DetectionRule.tenant_id == user.tenant_id)
    query = not_deleted(query, DetectionRule)
    if rule_status:
        query = query.filter(DetectionRule.status == rule_status)
    if level:
        query = query.filter(DetectionRule.level == level)
    if search:
        query = query.filter(DetectionRule.title.ilike(f"%{search}%"))
    return query.order_by(DetectionRule.updated_at.desc()).offset(offset).limit(limit).all()


@router.post("", response_model=DetectionRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: DetectionRuleIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> DetectionRuleOut:
    # Validate the YAML before saving
    validation = _validate_sigma_yaml(payload.detection_yaml)
    if not validation.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Invalid Sigma rule", "errors": validation.errors},
        )

    data = payload.model_dump()
    # Serialize list fields to JSON
    for field in ("mitre_attack_ids", "false_positives", "tags"):
        if data.get(field) is not None:
            data[field] = json.dumps(data[field])

    # The author is whoever is signed in, never what the body claims. Passing both
    # raised "got multiple values for keyword argument 'author'", so every create
    # 500'd — including every save from the detection editor.
    data.pop("author", None)
    rule = DetectionRule(**data, tenant_id=user.tenant_id, author=user.display_name)
    db.add(rule)
    db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            tenant_id=user.tenant_id,
            action="detection_rule.created",
            resource_type="detection_rule",
            resource_id=str(rule.id),
            detail=f"Created rule: {rule.title}",
        )
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/{rule_id}", response_model=DetectionRuleOut)
async def get_rule(
    rule_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> DetectionRuleOut:
    rule = (
        db.query(DetectionRule)
        .filter(
            DetectionRule.id == rule_id,
            DetectionRule.tenant_id == user.tenant_id,
            DetectionRule.deleted_at.is_(None),
        )
        .first()
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Detection rule not found")
    return rule


@router.patch("/{rule_id}", response_model=DetectionRuleOut)
async def update_rule(
    payload: DetectionRuleUpdate,
    rule_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> DetectionRuleOut:
    rule = (
        db.query(DetectionRule)
        .filter(
            DetectionRule.id == rule_id,
            DetectionRule.tenant_id == user.tenant_id,
            DetectionRule.deleted_at.is_(None),
        )
        .first()
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Detection rule not found")

    data = payload.model_dump(exclude_unset=True)

    # Validate YAML if being updated
    if "detection_yaml" in data:
        validation = _validate_sigma_yaml(data["detection_yaml"])
        if not validation.valid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"message": "Invalid Sigma rule", "errors": validation.errors},
            )

    # Serialize list fields
    for field in ("mitre_attack_ids", "false_positives", "tags"):
        if field in data and data[field] is not None:
            data[field] = json.dumps(data[field])

    for field, value in data.items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_rule(
    rule_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
):
    rule = (
        db.query(DetectionRule)
        .filter(
            DetectionRule.id == rule_id,
            DetectionRule.tenant_id == user.tenant_id,
            DetectionRule.deleted_at.is_(None),
        )
        .first()
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Detection rule not found")
    rule.soft_delete()
    db.add(
        AuditLog(
            user_id=user.id,
            tenant_id=user.tenant_id,
            action="detection_rule.deleted",
            resource_type="detection_rule",
            resource_id=str(rule.id),
        )
    )
    db.commit()
