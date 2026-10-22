"""GET /range-credentials/template-issues: the templates ADR 0013 §3 no longer accepts.

A typed-in account password is allowed only on a ``scenario: true`` account, never on a
staff login. Templates stored before that rule still carry such passwords: their accounts
no longer validate, so ranges of them cannot be provisioned (scheduled bookings included)
until the template is fixed. This lists them, with what to fix and how many upcoming
bookings would fail, so admins hear of it before a booking does (adversarial review of #250).
The migration logs the same list (``migrate_0013.templates_needing_scenario``) and the API
logs it at every start.

=====================================  ==================================================
Endpoint                               Permission
=====================================  ==================================================
GET /range-credentials/template-issues  template:update; the caller's tenant's templates,
                                        global ones too for an admin. Never a password.
=====================================  ==================================================
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import UserRole
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import service

router = APIRouter(prefix="/range-credentials", tags=["ranges"])


class TemplateIssueOut(BaseModel):
    template_id: uuid.UUID
    name: str
    version: str
    tenant_id: uuid.UUID | None = Field(None, description="None: a global template")
    problems: list[str] = Field(description="Which accounts break the rule, and why (no passwords)")
    upcoming_bookings: int = Field(description="Scheduled bookings of ranges built from it that would fail")


@router.get("/template-issues", response_model=list[TemplateIssueOut])
def list_template_issues(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_UPDATE)),
) -> list[TemplateIssueOut]:
    """Templates whose typed-in passwords are not on scenario accounts (ADR 0013 §3): fix each
    by marking the account ``scenario: true`` (its weak password is the lesson) or using
    ``generate: true``. **Permission: template:update**; an admin also sees global templates."""
    issues = service.template_issues(db, tenant_uuid(user), include_global=user.role == UserRole.admin)
    return [TemplateIssueOut(**i) for i in issues]
