"""TrueNorth Range — single sign-on hand-off into the tenant's Moodle.

TrueNorth is the source of truth for who may open a course, so Moodle has no login of
its own: the app mints a short-lived, single-use ticket signed with the LTI tool key,
and the browser POSTs it to ``local_truenorth``'s ``sso.php`` on that Moodle. The
plugin already trusts this key (``local_truenorth\\ticket``, ``typ`` = "sso"), checks
audience, lifetime and jti replay, then signs the Student in — creating their Moodle
account and enrolment on the spot — and opens the course.

The ``course`` claim is the TrueNorth course id, which is the ``idnumber`` the release
publisher gives the live Moodle course (``course_publishing.service``: ``live_idnumber``).

Moodle never calls TrueNorth or an identity provider for this, which keeps every farm
node free of outbound calls to private addresses (see ``docs/moodle-primer.md``).
"""

from __future__ import annotations

import time
import uuid

import jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import lti13
from .auth import CurrentUser
from .course_publishing.models import PUBLISHED, CoursePublication
from .models import Course, Enrollment, EnrollmentStatus, ExternalPlatform, User
from .rbac import Permission, user_has_permission
from .tenancy import get_owned_or_global, tenant_uuid

ISSUER = "truenorth"
TICKET_SECONDS = 60
SSO_PATH = "/local/truenorth/sso.php"

# A Student may enter a course they are on, or have finished; not one they left.
ACTIVE = (EnrollmentStatus.enrolled, EnrollmentStatus.in_progress, EnrollmentStatus.completed)


class MoodleSsoIn(BaseModel):
    course_id: uuid.UUID | None = None


class MoodleSsoOut(BaseModel):
    """POST ``token`` (form field) to ``action``. Never put it in a URL."""

    action: str
    token: str


class NotAvailableError(Exception):
    """The caller may not open this course in Moodle (maps to 403/404)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def tenant_moodle(db: Session, user: CurrentUser) -> ExternalPlatform:
    """The caller's tenant's Moodle: one farm instance per tenant (the oldest active one)."""
    platform = (
        db.query(ExternalPlatform)
        .filter(
            ExternalPlatform.tenant_id == tenant_uuid(user),
            ExternalPlatform.platform_type == "moodle",
            ExternalPlatform.is_active.is_(True),
        )
        .order_by(ExternalPlatform.created_at)
        .first()
    )
    if not platform or not platform.lti_issuer:
        raise NotAvailableError(404, "No Moodle is set up for your unit")
    return platform


def course_available(db: Session, user: CurrentUser, course_id: uuid.UUID) -> bool:
    """Whether "Open in Moodle" can land on this course for the caller's tenant.

    True when the tenant has a usable Moodle (``tenant_moodle``) AND the course is live
    on it: the ticket's ``course`` claim is looked up in Moodle by ``idnumber``, which only
    a published release creates (``live_idnumber`` = course id). Without a publication,
    ``sso.php`` fails with ``ssocoursemissing``. Enrolment is not part of this: an
    un-enrolled Student gets a clear 403 from ``/integrations/moodle/sso``.
    """
    try:
        platform = tenant_moodle(db, user)
    except NotAvailableError:
        return False
    return (
        db.query(CoursePublication.id)
        .filter(
            CoursePublication.course_id == course_id,
            CoursePublication.platform_id == platform.id,
            CoursePublication.state == PUBLISHED,
        )
        .first()
        is not None
    )


def moodle_role(db: Session, user: CurrentUser, course_id: uuid.UUID | None) -> str:
    """'teacher' for staff, 'student' for someone actively enrolled; else refuse."""
    if course_id is None:
        return "teacher" if user_has_permission(user, Permission.LEARNING_RECORD_WRITE) else "student"
    course = get_owned_or_global(db, Course, course_id, user, not_found="Course not found")
    if user_has_permission(user, Permission.LEARNING_RECORD_WRITE):
        return "teacher"
    enrolled = (
        db.query(Enrollment.id)
        .filter(
            Enrollment.user_id == uuid.UUID(str(user.id)),
            Enrollment.course_id == course.id,
            Enrollment.status.in_(ACTIVE),
        )
        .first()
    )
    if not enrolled:
        raise NotAvailableError(403, "You are not enrolled in this course")
    return "student"


def mint_ticket(db: Session, user: CurrentUser, course_id: uuid.UUID | None = None) -> MoodleSsoOut:
    """The form the browser POSTs to Moodle: ``action`` and a one-minute ``token``."""
    platform = tenant_moodle(db, user)
    role = moodle_role(db, user, course_id)
    person = db.get(User, uuid.UUID(str(user.id)))  # tenant-safe: the caller's own row
    first = (person.first_name if person else None) or ""
    last = (person.last_name if person else None) or ""
    if not first and not last:
        first, _, last = (user.display_name or user.email).partition(" ")

    now = int(time.time())
    audience = platform.lti_issuer.rstrip("/")
    claims = {
        "iss": ISSUER,
        "typ": "sso",
        "aud": audience,
        # local_truenorth refuses a ticket whose tid is not the tenant it serves: one tool
        # key signs for every tenant, so the audience alone cannot bind it.
        "tid": str(platform.tenant_id),
        "sub": str(user.id),
        "email": user.email,
        "given_name": first or user.email,
        "family_name": last or "-",
        "role": role,
        "iat": now,
        "exp": now + TICKET_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    if course_id is not None:
        claims["course"] = str(course_id)
    key = lti13.get_tool_key(db)
    token = jwt.encode(claims, lti13.signing_pem(key), algorithm="RS256", headers={"kid": key.kid})
    return MoodleSsoOut(action=audience + SSO_PATH, token=token)
