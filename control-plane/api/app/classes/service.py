"""Classes and their roll call: validation, the roll-call rules and its emails (ADR 0014).

Adding an email to a roll call (``add_to_roll_call``):

- an active Student of the class's tenant with that email is linked and enrolled on the
  class's course at once (#272: a class is enrolled in one action, not fifteen);
- a staff account of the tenant is refused: a roll call holds Students only;
- anyone else (no account yet, a roster row that has never signed in, or an address
  another tenant uses, which is not disclosed) stays ``invited`` and is emailed to onboard.
  Their first AD sign-in admits them (``onboarding.admit_from_roll_call``).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..enrollment import ensure_enrollment
from ..models import AuditLog, Course, User, UserRole
from .models import Class, ClassStaff, RollCallEntry, RollCallStatus, StaffRole
from .schemas import MAX_ROLL_CALL_BATCH, ClassIn, RollCallRefusal

logger = logging.getLogger("truenorth.api.classes")

# Deliberately plain: one @, no spaces, a dot in the domain. The IdP decides what a real
# address is; this only keeps typing slips and stray words off the roll call.
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_SPLIT = re.compile(r"[,;\s]+")
_WRAP = "<>\"'()[]"
MAX_CSV_BYTES = 1_000_000

LEAD_ROLES = (UserRole.instructor, UserRole.admin)
ROLES_FOR = {
    StaffRole.assistant: (UserRole.instructor, UserRole.admin),
    StaffRole.observer: (UserRole.observer, UserRole.instructor, UserRole.admin),
}


def normalise_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _now() -> datetime:
    return datetime.now(UTC)


# ── Parsing a paste or a CSV ───────────────────────────────────────────────


def parse_emails(text: str) -> tuple[list[str], list[str]]:
    """(emails, invalid) from pasted text. Words without an @ (names) are skipped; a piece
    with an @ that is not an address is reported. Emails are lower-cased, de-duplicated,
    in the order given."""
    emails: list[str] = []
    invalid: list[str] = []
    for raw in _SPLIT.split(text or ""):
        token = raw.strip(_WRAP)
        if not token or "@" not in token:
            continue
        if token.lower().startswith("mailto:"):
            token = token[7:]
        if EMAIL_RE.match(token):
            e = token.lower()
            if e not in emails:
                emails.append(e)
        elif token not in invalid:
            invalid.append(token[:320])
    return emails, invalid


def parse_csv(raw: bytes) -> tuple[list[str], list[str]]:
    """(emails, invalid) from a CSV. With an ``email`` header, that column; otherwise every
    cell is read as pasted text, so a one-column list with no header works too."""
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "The CSV is too large (1 MB at most)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "The CSV must be UTF-8 text") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    header = [c.strip().lower() for c in rows[0]]
    if "email" in header:
        col = header.index("email")
        cells = [r[col] for r in rows[1:] if len(r) > col]
    else:
        cells = [c for r in rows for c in r]
    return parse_emails("\n".join(cells))


# ── The class record ───────────────────────────────────────────────────────


def tenant_person(db: Session, tenant_id: uuid.UUID, user_id: uuid.UUID, roles: tuple[UserRole, ...], what: str) -> User:
    """An active account of ``tenant_id`` holding one of ``roles``, or 422. Another
    tenant's account reads as unknown, never as "exists elsewhere"."""
    u = (
        db.query(User)
        .filter(User.id == user_id, User.tenant_id == tenant_id, User.deleted_at.is_(None))
        .first()
    )
    if u is None or not u.is_active:
        raise HTTPException(422, f"The {what} must be an active account of this organisation")
    if u.role not in roles:
        allowed = ", ".join(r.value for r in roles)
        raise HTTPException(422, f"The {what} must hold one of these roles: {allowed}")
    return u


def course_for(db: Session, tenant_id: uuid.UUID, course_id: uuid.UUID) -> Course:
    """The tenant's own course or a shared catalogue course; anything else is 422."""
    c = (
        db.query(Course)
        .filter(Course.id == course_id, (Course.tenant_id == tenant_id) | (Course.tenant_id.is_(None)))
        .first()
    )
    if c is None:
        raise HTTPException(422, "Course not found")
    return c


def apply(db: Session, cls: Class, body: ClassIn, actor: CurrentUser) -> None:
    """Validate ``body`` against the class's tenant and write it onto ``cls`` (new or
    existing), with its staff. Does not commit."""
    tid = cls.tenant_id
    course_for(db, tid, body.course_id)
    if cls.course_id is not None and cls.course_id != body.course_id and _has_linked_students(db, cls):
        raise HTTPException(
            409, "Students on this roll call are already enrolled on its course; the course can no longer change"
        )
    lead_id = body.lead_instructor_id
    if lead_id is None:
        lead_id = cls.lead_instructor_id or uuid.UUID(actor.id)
    tenant_person(db, tid, lead_id, LEAD_ROLES, "lead instructor")
    assistants, observers = list(dict.fromkeys(body.assistant_ids)), list(dict.fromkeys(body.observer_ids))
    if lead_id in assistants or lead_id in observers:
        raise HTTPException(422, "The lead instructor is not also an assistant or observer")
    if set(assistants) & set(observers):
        raise HTTPException(422, "Someone is listed both as an assistant and as an observer")
    for uid in assistants:
        tenant_person(db, tid, uid, ROLES_FOR[StaffRole.assistant], "assistant instructor")
    for uid in observers:
        tenant_person(db, tid, uid, ROLES_FOR[StaffRole.observer], "observer")

    # Validated: only now does anything change (a refused create leaves no row behind).
    cls.name = body.name
    cls.course_id = body.course_id
    cls.start_date = body.start_date
    cls.end_date = body.end_date
    cls.lead_instructor_id = lead_id
    db.add(cls)
    db.flush()

    wanted = {uid: StaffRole.assistant.value for uid in assistants} | {uid: StaffRole.observer.value for uid in observers}
    for row in db.query(ClassStaff).filter(ClassStaff.class_id == cls.id).all():
        # A pure dependent with no history of its own (ADR 0010 §3): a staff link.
        if row.user_id not in wanted:
            db.delete(row)
        else:
            row.role = wanted.pop(row.user_id)
    for uid, role in wanted.items():
        db.add(ClassStaff(class_id=cls.id, user_id=uid, role=role))
    db.flush()


def _has_linked_students(db: Session, cls: Class) -> bool:
    return (
        db.query(RollCallEntry.id)
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.user_id.isnot(None))
        .first()
        is not None
    )


def audit(db: Session, actor_id: uuid.UUID | str | None, cls: Class, action: str, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(str(actor_id)) if actor_id else None,
            tenant_id=cls.tenant_id,
            action=action,
            resource_type="class",
            resource_id=str(cls.id),
            detail=detail or None,
        )
    )


# ── The roll call ──────────────────────────────────────────────────────────


def _user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(func.lower(User.email) == email, User.deleted_at.is_(None)).first()


def is_pre_provisioned(u: User) -> bool:
    """A roster row (CSV import) that no directory identity has claimed yet."""
    return u.source == "csv_import"


@dataclass
class AddResult:
    added: list[RollCallEntry]
    already: list[str]
    refused: list[RollCallRefusal]
    to_onboard: list[RollCallEntry]


def add_to_roll_call(db: Session, cls: Class, actor: CurrentUser, emails: list[str]) -> AddResult:
    """Add ``emails`` (already normalised) to the roll call. Does not commit."""
    if len(emails) > MAX_ROLL_CALL_BATCH:
        raise HTTPException(422, f"At most {MAX_ROLL_CALL_BATCH} emails at a time")
    res = AddResult(added=[], already=[], refused=[], to_onboard=[])
    for email in emails:
        entry = db.query(RollCallEntry).filter(RollCallEntry.class_id == cls.id, RollCallEntry.email == email).first()
        if entry is not None and entry.status != RollCallStatus.removed.value:
            res.already.append(email)
            continue
        account = _user_by_email(db, email)
        here = account is not None and account.tenant_id == cls.tenant_id
        if here and account.role != UserRole.student:
            res.refused.append(RollCallRefusal(email=email, reason=f"belongs to a {account.role.value} account; a roll call holds Students only"))
            continue
        if here and not account.is_active:
            res.refused.append(RollCallRefusal(email=email, reason="belongs to a deactivated account"))
            continue
        if entry is None:
            entry = RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email=email)
            db.add(entry)
        entry.added_by = uuid.UUID(actor.id)
        entry.removed_at = None
        entry.user_id = None
        entry.signed_in_at = None
        entry.status = RollCallStatus.invited.value
        if here:
            entry.user_id = account.id
            ensure_enrollment(db, user_id=account.id, course_id=cls.course_id, tenant_id=cls.tenant_id)
            if not is_pre_provisioned(account):
                entry.status = RollCallStatus.signed_in.value
                entry.signed_in_at = _now()
        if entry.status == RollCallStatus.invited.value and entry.onboarding_sent_at is None:
            res.to_onboard.append(entry)
        res.added.append(entry)
    db.flush()
    if res.added:
        audit(db, actor.id, cls, "roll_call_add", f"added {len(res.added)}: {', '.join(e.email for e in res.added)}"[:2000])
    return res


def remove_from_roll_call(db: Session, cls: Class, entry: RollCallEntry, actor: CurrentUser) -> None:
    """Take a Student off the roll call. The entry stays, marked removed (ADR 0010). Their
    account and enrolment are untouched: they may be on the course for other reasons."""
    entry.status = RollCallStatus.removed.value
    entry.removed_at = _now()
    audit(db, actor.id, cls, "roll_call_remove", entry.email)


def class_roll_call_emails(db: Session, cls: Class) -> list[str]:
    """Who a class's invites and reminders go to (ADR 0014; #291 review on #278): the roll
    call, not course enrolment.

    An entry linked to an account counts only while that account is an active Student of
    the class's own tenant, with the account's current email. A Student who moved tenant
    is dropped: a class belongs to one tenant, and its mail goes nowhere else. An entry
    with no account yet is the address the lead typed. Removed entries never count.

    TODO(#281): scheduler/invites.recipients uses this for bookings linked to a class.
    """
    out: set[str] = set()
    rows = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.status != RollCallStatus.removed.value)
        .all()
    )
    for e in rows:
        if e.user_id is None:
            out.add(e.email)
            continue
        # tenant-safe: checked against the class's tenant on the next line.
        u = db.get(User, e.user_id)
        if u is None or u.tenant_id != cls.tenant_id or not u.is_active or u.deleted_at is not None:
            continue
        if u.role != UserRole.student:
            continue
        out.add(normalise_email(u.email))
    return sorted(out)


# ── Emails ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str


def sign_in_url() -> str:
    """Where Students sign in: TRUENORTH_WEB_URL, else https://DOMAIN, else LTI_WEB_BASE_URL."""
    explicit = os.getenv("TRUENORTH_WEB_URL", "").strip()
    domain = os.getenv("DOMAIN", "").strip()
    return explicit or (f"https://{domain}" if domain else os.getenv("LTI_WEB_BASE_URL", "").strip())


def _dates(cls: Class) -> str:
    if cls.start_date == cls.end_date:
        return f"on {cls.start_date:%A %d %B %Y}"
    return f"from {cls.start_date:%A %d %B %Y} to {cls.end_date:%A %d %B %Y}"


def _how_to_join() -> str:
    url = sign_in_url()
    where = f" at {url}" if url else ""
    return (
        f"Sign in to TrueNorth Range{where} with your usual network (AD) account. You are already approved: "
        "there is no access request to fill in, and you will be enrolled on the course when you first sign in.\n"
    )


def onboarding_mail(cls: Class, course_name: str, email: str) -> Mail:
    return Mail(
        to=email,
        subject=f"You are on the roll call for {cls.name}",
        body=(
            f"You have been added to the class {cls.name} ({course_name}), {_dates(cls)}.\n\n"
            + _how_to_join()
        ),
    )


def reminder_mail(cls: Class, course_name: str, email: str) -> Mail:
    return Mail(
        to=email,
        subject=f"Reminder: {cls.name} starts tomorrow",
        body=(
            f"Your class {cls.name} ({course_name}) starts tomorrow, {cls.start_date:%A %d %B %Y}.\n\n"
            "You have not signed in to TrueNorth Range yet. " + _how_to_join()
        ),
    )


async def send(mails: list[Mail]) -> None:
    """Through the scheduler's SMTP channel. At most once: the entry was marked sent before
    this runs, so a failure is logged, never retried into duplicates."""
    if not mails:
        return
    from ..notifications import get_channel

    channel = get_channel("email")
    for m in mails:
        try:
            if not await channel.send(m.to, m.subject, m.body):
                logger.warning("Class email (%s) was not delivered", m.subject)
        except Exception:
            logger.exception("Class email (%s) failed", m.subject)
