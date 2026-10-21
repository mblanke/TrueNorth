"""A range's credentials: the list, one password's reveal, an admin's rotation (ADR 0013 §3).

==================================================  =========================================
Endpoint                                            Permission(s)
==================================================  =========================================
GET  /ranges/{range_id}/credentials                 RANGE_READ, then: range:credentials_read
                                                    (admin, instructor, range_ops): every
                                                    account; Student: student_visible
                                                    accounts of a range they take part in;
                                                    else 403. No passwords.
POST /ranges/{range_id}/credentials/{id}/reveal     as the list, for that one account.
                                                    Audited (range_credential_reveal).
POST /ranges/{range_id}/credentials/{id}/rotate     range:credentials_rotate (admin).
                                                    Audited (range_credential_rotate).
==================================================  =========================================

Who sees what is ``service.py``'s docstring. A Student's own lab session is also reached
through /lab-sessions/{id}/credentials (routers/lab_sessions.py). Responses carrying a
password are ``Cache-Control: no-store``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import service
from .models import RangeCredential

router = APIRouter(prefix="/ranges", tags=["ranges"])


class RangeCredentialOut(BaseModel):
    id: uuid.UUID = Field(description="The account's id: POST .../credentials/{id}/reveal shows its password")
    machine: str = Field(description="The template node id (or asset role) the account is on")
    username: str
    role: str = Field(description="admin | user")
    source: Literal["declared", "implicit"] = Field(
        "declared", description="declared: the range template's accounts:. implicit: a staff login TrueNorth "
                                "adds so the machine does not keep its golden template's build password "
                                "(built-in Administrator, Linux build user, soadmin)")
    student_visible: bool = Field(description="Handed to Students who take part in the range")
    generated: bool = Field(description="TrueNorth made the password up for this range")
    scenario: bool = Field(False, description="A typed-in password the template author chose on purpose (the "
                                              "lesson): authored by design, never rotated")
    applied: bool = Field(description="The last build or rotation reported setting it. False: not set by "
                                      "TrueNorth (Windows accounts other than Administrator, appliances, other "
                                      "backends, a pending rotation, or not built yet). Students only ever get "
                                      "applied accounts")
    setup_status: Literal["applied", "being_set_up", "not_set"] = Field(
        "not_set", description="applied: verified set. being_set_up (staff only): its machine's deploy-time "
                              "setup (product role image / Security Onion) is still running, verified later by the "
                              "reconcile task. not_set: not set by TrueNorth or not built yet")
    rotated_at: datetime | None = Field(None, description="When its password was last replaced (rebuild, reset, "
                                                          "admin rotation)")
    notes: str = ""


class RangeCredentialsOut(BaseModel):
    range_id: uuid.UUID
    scope: str = Field(description="all: every account (staff); student: only those handed to Students")
    accounts: list[RangeCredentialOut]


class RevealedCredentialOut(BaseModel):
    id: uuid.UUID
    machine: str
    username: str
    password: str


class RotateIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Why, for the audit log")


class RotateOut(BaseModel):
    account: RangeCredentialOut
    applies: Literal["in_guest", "next_build"] = Field(
        description="in_guest: the worker sets it in the running range's guests now (the account shows applied "
                    "once verified). next_build: the range is not built; its next build sets it")
    operation_id: str = Field(description="Logged by the worker task and in the audit row")
    dispatched: bool = Field(description="False: the task queue was not reachable; rotate again later")


def account_out(r: RangeCredential, pending: set[str]) -> RangeCredentialOut:
    return RangeCredentialOut(
        id=r.id, machine=r.machine, username=r.username, role=r.role, source=r.source or "declared",
        student_visible=r.student_visible, generated=r.generated, scenario=bool(r.scenario), applied=r.applied,
        setup_status=service.setup_status(r, pending), rotated_at=r.rotated_at, notes=r.notes or "")


@router.get(
    "/{range_id}/credentials",
    response_model=RangeCredentialsOut,
    responses={
        403: {"description": "The caller's role sees no range credentials (observers)"},
        404: {"description": "No such range in the caller's tenant, or a Student who does not take part in it"},
    },
)
def get_range_credentials(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> RangeCredentialsOut:
    """The accounts on a range's machines, as the caller may see them, **without passwords**
    (POST ``.../credentials/{id}/reveal`` shows one, audited).

    **Permission: range:read**, then by role: staff with ``range:credentials_read`` see every
    account (``applied`` false: not set by TrueNorth); a Student sees the ``student_visible``
    accounts the last build or rotation set, of a range they take part in (an exercise
    running on it whose booked course they are rostered on, or their own live lab session)
    and gets 404 for any other; an observer gets 403.
    """
    scope = service.access_scope(user)
    rng = service.readable_range(db, range_id, user, scope)
    rows = service.visible_accounts(db, rng, scope)
    pending = service.setup_pending_machines(rng) if scope == service.ALL else set()
    return RangeCredentialsOut(range_id=rng.id, scope=scope, accounts=[account_out(r, pending) for r in rows])


@router.post(
    "/{range_id}/credentials/{credential_id}/reveal",
    response_model=RevealedCredentialOut,
    responses={
        403: {"description": "The caller's role sees no range credentials (observers)"},
        404: {"description": "No such range or account the caller may see"},
    },
)
def reveal_range_credential(
    response: Response,
    range_id: uuid.UUID = Path(...),
    credential_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> RevealedCredentialOut:
    """One account's password. **Permission: range:read**, then as the list: staff any
    account of the range, a Student only one handed to them on a range they take part in.
    Every call writes an audit row (``range_credential_reveal``: who, the range, machine and
    username, the scope, and the Student's exercise or lab session), never the password."""
    scope = service.access_scope(user)
    rng = service.readable_range(db, range_id, user, scope)
    row = service.account(db, rng, credential_id, scope)
    context = (service.participation(db, user, rng) or {}) if scope == service.STUDENT else {}
    password = service.reveal(db, rng, row, scope, user, "range", context)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return RevealedCredentialOut(id=row.id, machine=row.machine, username=row.username, password=password)


@router.post(
    "/{range_id}/credentials/{credential_id}/rotate",
    response_model=RotateOut,
    status_code=202,
    responses={
        404: {"description": "No such range or account in the caller's tenant"},
        409: {"description": "A scenario account (never rotated), or a range no rotation can reach now"},
    },
)
def rotate_range_credential(
    body: RotateIn,
    range_id: uuid.UUID = Path(...),
    credential_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_CREDENTIALS_ROTATE)),
) -> RotateOut:
    """Give one account a new generated password. **Permission: range:credentials_rotate**
    (admin). A built range that is ready or running gets it in its guests now (worker task
    ``rotate_range_credentials``); an unbuilt one at its next build. Audited
    (``range_credential_rotate``, with the reason). Staff accounts change only this way."""
    rng = service.readable_range(db, range_id, user, service.ALL)
    row = service.account(db, rng, credential_id, service.ALL)
    operation_id = str(uuid.uuid4())
    in_guest = service.rotate(db, rng, row, user, body.reason, operation_id)
    db.commit()
    dispatched = True
    if in_guest:
        from ..celery_client import dispatch

        dispatched = dispatch("rotate_range_credentials", str(rng.id), [str(row.id)], operation_id) is not None
    db.refresh(row)
    return RotateOut(account=account_out(row, service.setup_pending_machines(rng)),
                     applies="in_guest" if in_guest else "next_build", operation_id=operation_id,
                     dispatched=dispatched)
