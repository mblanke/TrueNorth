"""GET /ranges/{range_id}/credentials (ADR 0012 §13).

=====================================  =========================================
Endpoint                               Permission(s)
=====================================  =========================================
GET /ranges/{range_id}/credentials     RANGE_READ, then: range:credentials_read
                                       (admin, instructor, range_ops): every
                                       account; Student: student_visible accounts
                                       of a range they take part in; else 403
=====================================  =========================================

Who sees what is ``service.py``'s docstring. Every read is audited without the password.
Responses are ``Cache-Control: no-store``: they carry passwords.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import service

router = APIRouter(prefix="/ranges", tags=["ranges"])


class RangeCredentialOut(BaseModel):
    machine: str = Field(description="The template node id (or asset role) the account is on")
    username: str
    password: str
    role: str = Field(description="admin | user")
    student_visible: bool = Field(description="Handed to Students who take part in the range")
    generated: bool = Field(description="TrueNorth made the password up for this range")
    applied: bool = Field(description="The last build reported setting it. False: not set by TrueNorth "
                                      "(Windows accounts other than Administrator, appliances, other backends, "
                                      "or not built yet). Students only ever get applied accounts")
    notes: str = ""


class RangeCredentialsOut(BaseModel):
    range_id: uuid.UUID
    scope: str = Field(description="all: every account (staff); student: only those handed to Students")
    accounts: list[RangeCredentialOut]


@router.get(
    "/{range_id}/credentials",
    response_model=RangeCredentialsOut,
    responses={
        403: {"description": "The caller's role sees no range credentials (observers)"},
        404: {"description": "No such range in the caller's tenant, or a Student who does not take part in it"},
    },
)
def get_range_credentials(
    response: Response,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> RangeCredentialsOut:
    """The accounts on a range's machines, with their passwords, as the caller may see them.

    **Permission: range:read**, then by role: staff with ``range:credentials_read`` see every
    account (``applied`` false: not set by TrueNorth); a Student sees the ``student_visible``
    accounts the last build set, of a range they take part in (an exercise running on it
    whose booked course they are rostered on; lab sessions are not covered yet) and gets 404
    for any other; an observer gets 403. Audited per read.
    """
    scope = service.access_scope(user)
    rng = service.readable_range(db, range_id, user, scope)
    rows = service.visible_accounts(db, rng, scope)
    out = RangeCredentialsOut(
        range_id=rng.id,
        scope=scope,
        accounts=[
            RangeCredentialOut(machine=r.machine, username=r.username, password=r.password or "", role=r.role,
                               student_visible=r.student_visible, generated=r.generated, applied=r.applied,
                               notes=r.notes or "")
            for r in rows
        ],
    )
    service.audit_read(db, user, rng, scope, rows)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return out
