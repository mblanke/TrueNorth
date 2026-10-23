"""Classes and their roll call (ADR 0014 §1, §2, §5, §8; issue #278, folds in #272).

===================================================  ==========================================
Endpoint                                             Who
===================================================  ==========================================
GET    /classes                                      any signed-in user: the classes they may see
GET    /classes/mine                                 class:read: classes they lead, assist, observe
POST   /classes                                      class:create, in the caller's tenant
GET    /classes/{id}                                 its staff, its roll call, admins
PUT    /classes/{id}                                 its lead or an admin
DELETE /classes/{id}                                 its lead or an admin: cancels it (ADR 0010)
GET    /classes/{id}/roll-call                       its lead, assistants, observers, admins
POST   /classes/{id}/roll-call                       its lead or an admin: emails or pasted text
POST   /classes/{id}/roll-call/csv                   its lead or an admin: a CSV upload
DELETE /classes/{id}/roll-call/{entry_id}            its lead or an admin: marks it removed
===================================================  ==========================================

Another tenant's class, or one the caller has no part in, is not found (404). Visibility
and roles are decided in ``access``; the roll-call rules in ``service``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import Course, User
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import access, service
from .models import Class, ClassStaff, ClassStatus, RollCallEntry, RollCallStatus
from .schemas import (
    ClassIn,
    ClassOut,
    ClassPerson,
    ClassRole,
    RollCallAddIn,
    RollCallAddOut,
    RollCallEntryOut,
)

logger = logging.getLogger("truenorth.api.classes.router")

router = APIRouter(prefix="/classes", tags=["classes"])

# The permission is the role's floor; the class decides the rest (access.py). Every lead is
# an instructor or admin, who hold class:create; assistants and observers hold class:read.
_CHANGE = require_permission(Permission.CLASS_CREATE)
_STAFF_READ = require_permission(Permission.CLASS_READ)


def _person(u: User | None) -> ClassPerson | None:
    return ClassPerson(id=u.id, display_name=u.display_name, email=u.email) if u is not None else None


def _out(db: Session, cls: Class, role: ClassRole | None) -> ClassOut:
    # tenant-safe: the class's own course, lead and staff, each checked against its tenant when set.
    course = db.get(Course, cls.course_id)
    lead = db.get(User, cls.lead_instructor_id)
    staff = (
        db.query(ClassStaff, User)
        .join(User, User.id == ClassStaff.user_id)
        .filter(ClassStaff.class_id == cls.id)
        .order_by(User.display_name)
        .all()
    )
    count = (
        db.query(func.count(RollCallEntry.id))
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.status != RollCallStatus.removed.value)
        .scalar()
    )
    return ClassOut(
        id=cls.id, tenant_id=cls.tenant_id, name=cls.name, course_id=cls.course_id,
        course_name=course.name if course else "", start_date=cls.start_date, end_date=cls.end_date,
        status=cls.status, lead_instructor=_person(lead),
        assistants=[_person(u) for s, u in staff if s.role == "assistant"],
        observers=[_person(u) for s, u in staff if s.role == "observer"],
        my_role=role, can_edit=role in access.EDITORS, roll_call_count=count or 0, created_at=cls.created_at,
    )


def _entry_out(e: RollCallEntry, names: dict[uuid.UUID, str]) -> RollCallEntryOut:
    return RollCallEntryOut(
        id=e.id, email=e.email, user_id=e.user_id, display_name=names.get(e.user_id) if e.user_id else None,
        status=e.status, onboarding_sent_at=e.onboarding_sent_at, reminded_at=e.reminded_at,
        signed_in_at=e.signed_in_at, removed_at=e.removed_at, created_at=e.created_at,
    )


def _names(db: Session, entries: list[RollCallEntry], tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    ids = {e.user_id for e in entries if e.user_id}
    if not ids:
        return {}
    rows = db.query(User.id, User.display_name).filter(User.id.in_(ids), User.tenant_id == tenant_id).all()
    return {uid: name for uid, name in rows}


def _editable(db: Session, class_id: uuid.UUID, user: CurrentUser) -> Class:
    cls, role = access.get_visible(db, class_id, user)
    access.require_editor(role)
    return cls


# ── Classes ────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ClassOut], operation_id="listClasses", summary="Classes the caller may see")
def list_classes(
    include_cancelled: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[ClassOut]:
    """Admins: every class of their tenant. Anyone else: the classes they lead, assist or
    observe, or are on the roll call of."""
    q = access.visible(db, user)
    if not include_cancelled:
        q = q.filter(Class.status == ClassStatus.open.value)
    rows = q.order_by(Class.start_date.desc(), Class.name).limit(500).all()
    return [_out(db, c, access.role_in(db, c, user)) for c in rows]


@router.get(
    "/mine", response_model=list[ClassOut], operation_id="listMyClasses", summary="Classes the caller staffs"
)
def list_my_classes(
    include_cancelled: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_READ)),
) -> list[ClassOut]:
    """The caller's own classes as lead, assistant instructor or observer.  **class:read**"""
    q = access.staffed_by(db, user)
    if not include_cancelled:
        q = q.filter(Class.status == ClassStatus.open.value)
    rows = q.order_by(Class.start_date.desc(), Class.name).limit(500).all()
    return [_out(db, c, access.role_in(db, c, user)) for c in rows]


@router.post("", response_model=ClassOut, status_code=201, operation_id="createClass", summary="Create a class")
def create_class(
    body: ClassIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_CREATE)),
) -> ClassOut:
    """In the caller's own tenant, always. An instructor leads it unless they name another
    instructor of the tenant.  **class:create**"""
    if body.lead_instructor_id is None and user.role not in service.LEAD_ROLES:
        raise HTTPException(422, "Name the lead instructor")
    cls = Class(tenant_id=tenant_uuid(user), created_by=uuid.UUID(user.id), status=ClassStatus.open.value)
    service.apply(db, cls, body, user)
    service.audit(db, user.id, cls, "class_create", cls.name)
    db.commit()
    db.refresh(cls)
    return _out(db, cls, access.role_in(db, cls, user))


@router.get("/{class_id}", response_model=ClassOut, operation_id="getClass", summary="One class")
def get_class(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ClassOut:
    cls, role = access.get_visible(db, class_id, user)
    return _out(db, cls, role)


@router.put("/{class_id}", response_model=ClassOut, operation_id="updateClass", summary="Change a class")
def update_class(
    class_id: uuid.UUID,
    body: ClassIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> ClassOut:
    """Its lead or an admin. Naming another lead hands the class over."""
    cls = _editable(db, class_id, user)
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "A cancelled class cannot be changed")
    before = cls.lead_instructor_id
    service.apply(db, cls, body, user)
    detail = cls.name if before == cls.lead_instructor_id else f"{cls.name}; lead {before} -> {cls.lead_instructor_id}"
    service.audit(db, user.id, cls, "class_update", detail)
    db.commit()
    db.refresh(cls)
    # None when the caller just handed their class to another lead.
    return _out(db, cls, access.role_in(db, cls, user))


@router.delete(
    "/{class_id}", status_code=204, response_class=Response, response_model=None,
    operation_id="cancelClass", summary="Cancel a class",
)
def cancel_class(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
):
    """Its lead or an admin. A class is history (who was invited, who joined), so it is
    cancelled, not deleted (ADR 0010); it leaves the lists and no longer admits anyone."""
    cls = _editable(db, class_id, user)
    if cls.status != ClassStatus.cancelled.value:
        cls.status = ClassStatus.cancelled.value
        service.audit(db, user.id, cls, "class_cancel", cls.name)
        db.commit()


# ── Roll call ──────────────────────────────────────────────────────────────


@router.get(
    "/{class_id}/roll-call", response_model=list[RollCallEntryOut], operation_id="getClassRollCall",
    summary="A class's roll call",
)
def get_roll_call(
    class_id: uuid.UUID,
    include_removed: bool = True,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_STAFF_READ),
) -> list[RollCallEntryOut]:
    """Its lead, assistants, observers and admins. Not the Students on it."""
    cls, role = access.get_visible(db, class_id, user)
    access.require_staff(role)
    q = db.query(RollCallEntry).filter(RollCallEntry.class_id == cls.id)
    if not include_removed:
        q = q.filter(RollCallEntry.status != RollCallStatus.removed.value)
    entries = q.order_by(RollCallEntry.email).all()
    names = _names(db, entries, cls.tenant_id)
    return [_entry_out(e, names) for e in entries]


def _add(
    db: Session, cls: Class, user: CurrentUser, emails: list[str], invalid: list[str], background: BackgroundTasks
) -> RollCallAddOut:
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "A cancelled class's roll call cannot change")
    res = service.add_to_roll_call(db, cls, user, emails)
    # tenant-safe: the class's own course.
    course = db.get(Course, cls.course_id)
    now = service._now()
    mails = []
    for entry in res.to_onboard:
        entry.onboarding_sent_at = now
        mails.append(service.onboarding_mail(cls, course.name if course else "the course", entry.email))
    db.commit()
    background.add_task(service.send, mails)
    names = _names(db, res.added, cls.tenant_id)
    return RollCallAddOut(
        added=[_entry_out(e, names) for e in res.added], already_on_roll_call=res.already,
        invalid=invalid, refused=res.refused,
    )


@router.post(
    "/{class_id}/roll-call", response_model=RollCallAddOut, operation_id="addToClassRollCall",
    summary="Add Students to a roll call by email or pasted list",
)
def add_to_roll_call(
    class_id: uuid.UUID,
    body: RollCallAddIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> RollCallAddOut:
    """Its lead or an admin. A Student with no account yet is emailed to onboard."""
    cls = _editable(db, class_id, user)
    emails, invalid = service.parse_emails("\n".join([*body.emails, body.text]))
    return _add(db, cls, user, emails, invalid, background)


@router.post(
    "/{class_id}/roll-call/csv", response_model=RollCallAddOut, operation_id="importClassRollCallCsv",
    summary="Add Students to a roll call from a CSV",
)
async def import_roll_call_csv(
    class_id: uuid.UUID,
    file: UploadFile,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> RollCallAddOut:
    """Its lead or an admin. An ``email`` column if the CSV has a header with one;
    otherwise every cell that is an email address."""
    cls = _editable(db, class_id, user)
    emails, invalid = service.parse_csv(await file.read(service.MAX_CSV_BYTES + 1))
    return _add(db, cls, user, emails, invalid, background)


@router.delete(
    "/{class_id}/roll-call/{entry_id}", status_code=204, response_class=Response, response_model=None,
    operation_id="removeFromClassRollCall", summary="Take a Student off a roll call",
)
def remove_from_roll_call(
    class_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
):
    """Its lead or an admin. The entry is kept, marked removed; account and enrolment stay."""
    cls = _editable(db, class_id, user)
    entry = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.id == entry_id, RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id)
        .first()
    )
    if entry is None:
        raise HTTPException(404, "Roll-call entry not found")
    if entry.status != RollCallStatus.removed.value:
        service.remove_from_roll_call(db, cls, entry, user)
        db.commit()

