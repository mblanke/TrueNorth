"""Who sees and who changes a class (ADR 0014 §5, §8).

- Everything is tenant-scoped: another tenant's class is not found (404), whoever asks.
- A class is visible only to its lead, its assistants and observers, the Students on its
  roll call (not removed), and the admins of its tenant. Anyone else in the tenant: 404.
- Only the lead or an admin changes the class or its roll call. Assistants and observers
  read the roll call. A Student on it sees the class, never the roll call (403).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Query, Session

from ..auth import CurrentUser
from ..models import UserRole
from ..tenancy import tenant_uuid
from .models import Class, ClassStaff, RollCallEntry, RollCallStatus
from .schemas import ClassRole

LEAD_ROLES = (UserRole.instructor, UserRole.admin)
EDITORS = (ClassRole.lead, ClassRole.admin)
STAFF = (ClassRole.lead, ClassRole.assistant, ClassRole.observer, ClassRole.admin)


def visible(db: Session, user: CurrentUser) -> Query:
    """The classes the caller may see, in the caller's tenant only."""
    q = db.query(Class).filter(Class.tenant_id == tenant_uuid(user))
    if user.role == UserRole.admin:
        return q
    uid = uuid.UUID(user.id)
    staffed = select(ClassStaff.class_id).where(ClassStaff.user_id == uid)
    on_roll_call = select(RollCallEntry.class_id).where(
        RollCallEntry.user_id == uid, RollCallEntry.status != RollCallStatus.removed.value
    )
    return q.filter(or_(Class.lead_instructor_id == uid, Class.id.in_(staffed), Class.id.in_(on_roll_call)))


def staffed_by(db: Session, user: CurrentUser) -> Query:
    """The caller's own classes as staff: lead, assistant or observer ("my classes")."""
    uid = uuid.UUID(user.id)
    staffed = select(ClassStaff.class_id).where(ClassStaff.user_id == uid)
    return db.query(Class).filter(
        Class.tenant_id == tenant_uuid(user), or_(Class.lead_instructor_id == uid, Class.id.in_(staffed))
    )


def role_from(cls: Class, user: CurrentUser, staff: dict, on_roll_call: bool) -> ClassRole | None:
    """The caller's part in ``cls`` from already-loaded facts (``staff``: user id -> role)."""
    if str(cls.tenant_id) != str(user.tenant_id):
        return None
    uid = uuid.UUID(user.id)
    # The lead counts as lead only while they still hold a lead's role (an instructor
    # demoted to Student keeps no rights over the class). The caller's role is the
    # database's, read on every request (get_current_user), and a disabled account
    # never gets that far.
    if cls.lead_instructor_id == uid and user.role in LEAD_ROLES:
        return ClassRole.lead
    if user.role == UserRole.admin:
        return ClassRole.admin
    if uid in staff and user.role != UserRole.student:
        return ClassRole(staff[uid])
    return ClassRole.student if on_roll_call else None


def role_in(db: Session, cls: Class, user: CurrentUser) -> ClassRole | None:
    """The caller's part in ``cls``, or None when they have none (and may not see it)."""
    if str(cls.tenant_id) != str(user.tenant_id):
        return None
    uid = uuid.UUID(user.id)
    staff = {
        s.user_id: s.role
        for s in db.query(ClassStaff).filter(ClassStaff.class_id == cls.id, ClassStaff.user_id == uid)
    }
    on = (
        db.query(RollCallEntry.id)
        .filter(
            RollCallEntry.class_id == cls.id,
            RollCallEntry.user_id == uid,
            RollCallEntry.status != RollCallStatus.removed.value,
        )
        .first()
    )
    return role_from(cls, user, staff, on is not None)


def get_visible(db: Session, class_id: uuid.UUID, user: CurrentUser) -> tuple[Class, ClassRole]:
    """The class and the caller's part in it, or 404 (another tenant's, or not theirs)."""
    cls = db.query(Class).filter(Class.id == class_id, Class.tenant_id == tenant_uuid(user)).first()
    role = role_in(db, cls, user) if cls is not None else None
    if cls is None or role is None:
        raise HTTPException(404, "Class not found")
    return cls, role


def require_editor(role: ClassRole) -> None:
    if role not in EDITORS:
        raise HTTPException(403, "Only the class's lead instructor or an admin can change it")


def require_staff(role: ClassRole) -> None:
    if role not in STAFF:
        raise HTTPException(403, "Only the class's instructors, observers and admins see its roll call")
