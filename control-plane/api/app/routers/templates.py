"""TrueNorth Range — Templates router.

CRUD operations for range templates (YAML definitions).

Permissions required per endpoint:

=======================================  ==========================
Endpoint                                 Permission(s)
=======================================  ==========================
POST   /templates                        TEMPLATE_CREATE
GET    /templates                        TEMPLATE_READ
GET    /templates/{template_id}          TEMPLATE_READ
PUT    /templates/{template_id}          TEMPLATE_UPDATE
DELETE /templates/{template_id}          TEMPLATE_DELETE
=======================================  ==========================
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import engine_bridge, range_topology
from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, Template, UserRole
from ..rbac import Permission, require_permission
from ..schemas import TemplateIn, TemplateListOut, TemplateOut, TemplateUpdate
from ..tenancy import get_owned

logger = logging.getLogger("truenorth.api.templates")

router = APIRouter(prefix="/templates", tags=["templates"])


class YamlValidateIn(BaseModel):
    yaml: str


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str) -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid))


@router.post("/validate")
def validate_template(
    body: YamlValidateIn,
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
) -> dict:
    """Validate range-template YAML against the engine's canonical schema."""
    return engine_bridge.validate_yaml("template", body.yaml)


@router.post("/{template_id}/diagram-preview")
def template_diagram_preview(
    template_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
) -> dict:
    """Render a template's declared assets into a starter JointJS diagram.

    Lets the Range Designer open a template as an editable starting topology
    rather than a blank canvas. 422 if the template YAML is unusable.
    """
    tmpl = get_owned(db, Template, template_id, user, not_found="Template not found")
    try:
        diagram = range_topology.build_template_diagram(tmpl.yaml or "")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"template_id": str(tmpl.id), "diagram_json": diagram}


@router.post("", response_model=TemplateOut, status_code=201)
def create_template(
    body: TemplateIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_CREATE)),
) -> Template:
    """Create a new template.  **Permission: template:create**"""
    tmpl = Template(
        name=body.name,
        version=body.version,
        yaml=body.yaml,
        is_public=body.is_public,
        tenant_id=uuid.UUID(user.tenant_id),
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    _audit(db, user, "create", "template", str(tmpl.id))
    db.commit()
    return tmpl


@router.get("", response_model=list[TemplateListOut])
def list_templates(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[Template]:
    """List templates visible to user (own-tenant + public).  **Permission: template:read**"""
    q = db.query(Template).filter(
        (Template.tenant_id == uuid.UUID(user.tenant_id)) | (Template.is_public == True)  # noqa: E712
    )
    return q.order_by(Template.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(
    template_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
) -> Template:
    """Retrieve a single template.  **Permission: template:read**"""
    tmpl = get_owned(db, Template, template_id, user, not_found="Template not found")
    return tmpl


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: uuid.UUID,
    body: TemplateUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_UPDATE)),
) -> Template:
    """Update an existing template.  **Permission: template:update**"""
    tmpl = get_owned(db, Template, template_id, user, not_found="Template not found")
    # Non-admins can only update own-tenant templates
    if tmpl.tenant_id and tmpl.tenant_id != uuid.UUID(user.tenant_id) and user.role != UserRole.admin:
        raise HTTPException(403, "Not authorized to update this template")
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(tmpl, key, value)
    db.commit()
    db.refresh(tmpl)
    _audit(db, user, "update", "template", str(tmpl.id))
    db.commit()
    return tmpl


@router.delete("/{template_id}", status_code=204, response_class=Response)
def delete_template(
    template_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_DELETE)),
):
    """Delete a template.  **Permission: template:delete**"""
    tmpl = get_owned(db, Template, template_id, user, not_found="Template not found")
    db.delete(tmpl)
    db.commit()
    _audit(db, user, "delete", "template", str(template_id))
    db.commit()
