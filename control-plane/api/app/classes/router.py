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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import AuditLog, Course, User
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import access, service
from .models import Class, ClassStaff, ClassStatus, RollCallEntry, RollCallSettings, RollCallStatus
from .schemas import (
    ClassIn,
    ClassOut,
    ClassPerson,
    ClassRole,
    RollCallAddIn,
    RollCallAddOut,
    RollCallDomainsIn,
    RollCallDomainsOut,
    RollCallEntryOut,
)

logger = logging.getLogger("truenorth.api.classes.router")

router = APIRouter(prefix="/classes", tags=["classes"])

# The permission is the role's floor; the class decides the rest (access.py). Every lead is
# an instructor or admin, who hold class:create; assistants and observers hold class:read.
_CHANGE = require_permission(Permission.CLASS_CREATE)
_STAFF_READ = require_permission(Permission.CLASS_READ)


def _person(u: User | None, *, with_email: bool) -> ClassPerson | None:
    if u is None:
        return None
    return ClassPerson(id=u.id, display_name=u.display_name, email=u.email if with_email else None)


def _outs(db: Session, classes: list[Class], user: CurrentUser) -> list[ClassOut]:
    """Classes as the caller sees them, in a fixed number of queries whatever their count."""
    if not classes:
        return []
    ids = [c.id for c in classes]
    uid = uuid.UUID(user.id)
    tid = tenant_uuid(user)
    # tenant-safe: courses, leads and staff of classes already scoped to the caller's tenant.
    courses = {c.id: c.name for c in db.query(Course).filter(Course.id.in_({c.course_id for c in classes}))}
    leads = {u.id: u for u in db.query(User).filter(User.id.in_({c.lead_instructor_id for c in classes}),
                                                    User.tenant_id == tid)}
    staff_rows = (
        db.query(ClassStaff, User)
        .join(User, User.id == ClassStaff.user_id)
        .filter(ClassStaff.class_id.in_(ids), User.tenant_id == tid)
        .order_by(User.display_name)
        .all()
    )
    counts = dict(
        db.query(RollCallEntry.class_id, func.count(RollCallEntry.id))
        .filter(RollCallEntry.class_id.in_(ids), RollCallEntry.status != RollCallStatus.removed.value)
        .group_by(RollCallEntry.class_id)
        .all()
    )
    on_roll_call = {
        cid for (cid,) in db.query(RollCallEntry.class_id).filter(
            RollCallEntry.class_id.in_(ids), RollCallEntry.user_id == uid,
            RollCallEntry.status != RollCallStatus.removed.value,
        )
    }
    out = []
    for c in classes:
        staff = [(s, u) for s, u in staff_rows if s.class_id == c.id]
        role = access.role_from(c, user, {s.user_id: s.role for s, _ in staff}, c.id in on_roll_call)
        # A Student sees who teaches the class, not their email addresses.
        with_email = role is not None and role != ClassRole.student
        out.append(ClassOut(
            id=c.id, tenant_id=c.tenant_id, name=c.name, course_id=c.course_id,
            course_name=courses.get(c.course_id, ""), start_date=c.start_date, end_date=c.end_date,
            status=c.status, lead_instructor=_person(leads.get(c.lead_instructor_id), with_email=with_email),
            assistants=[_person(u, with_email=with_email) for s, u in staff if s.role == "assistant"],
            observers=[_person(u, with_email=with_email) for s, u in staff if s.role == "observer"],
            my_role=role, can_edit=role in access.EDITORS, roll_call_count=counts.get(c.id, 0),
            created_at=c.created_at,
        ))
    return out


def _out(db: Session, cls: Class, user: CurrentUser) -> ClassOut:
    return _outs(db, [cls], user)[0]


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
    return _outs(db, rows, user)


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
    return _outs(db, rows, user)


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
    return _out(db, cls, user)


@router.get(
    "/roll-call-domains", response_model=RollCallDomainsOut, operation_id="getRollCallDomains",
    summary="Email domains this organisation's roll calls accept",
)
def get_roll_call_domains(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> RollCallDomainsOut:
    """The admin's setting (an empty setting: any domain, in smaller batches), or else the
    domains of the organisation's own AD accounts. No setting and no AD account: nothing
    may be added until an admin sets it.  **class:create**"""
    domains, source = service.allowed_domains(db, tenant_uuid(user))
    return RollCallDomainsOut(email_domains=domains, source=source)


@router.put(
    "/roll-call-domains", response_model=RollCallDomainsOut, operation_id="setRollCallDomains",
    summary="Set the email domains this organisation's roll calls accept",
)
def set_roll_call_domains(
    body: RollCallDomainsIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_UPDATE)),
) -> RollCallDomainsOut:
    """For the caller's own organisation only. An empty list allows any domain.  **tenant:update**"""
    tid = tenant_uuid(user)
    # tenant-safe: keyed by the caller's own tenant id.
    row = db.get(RollCallSettings, tid)
    if row is None:
        row = RollCallSettings(tenant_id=tid)
        db.add(row)
    row.email_domains = body.email_domains
    row.updated_by = uuid.UUID(user.id)
    db.add(AuditLog(user_id=uuid.UUID(user.id), tenant_id=tid, action="roll_call_domains_set",
                    resource_type="tenant", resource_id=str(tid), detail=", ".join(body.email_domains) or "(any)"))
    db.commit()
    return RollCallDomainsOut(email_domains=body.email_domains, source="setting")


@router.get("/{class_id}", response_model=ClassOut, operation_id="getClass", summary="One class")
def get_class(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ClassOut:
    cls, _ = access.get_visible(db, class_id, user)
    return _out(db, cls, user)


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
    return _out(db, cls, user)


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
    try:
        res = service.add_to_roll_call(db, cls, user, emails)
        # tenant-safe: the class's own course.
        course = db.get(Course, cls.course_id)
        now = service._now()
        mails = []
        for entry in res.to_onboard:
            entry.onboarding_sent_at = now
            mails.append(service.onboarding_mail(cls, course.name if course else "the course", entry.email))
        db.commit()
    except IntegrityError:
        # Someone else added the same address to this roll call at the same moment.
        db.rollback()
        raise HTTPException(409, "The roll call changed while this was being added; reload and try again") from None
    background.add_task(service.send, mails)
    names = _names(db, res.added, cls.tenant_id)
    return RollCallAddOut(
        added=[_entry_out(e, names) for e in res.added], already_on_roll_call=res.already,
        invalid=invalid, refused=res.refused, not_emailed=res.not_emailed,
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

