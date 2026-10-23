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
from dataclasses import dataclass, field
from datetime import UTC, datetime, time

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..enrollment import ensure_enrollment
from ..models import AuditLog, Course, User, UserRole
from . import sessions
from .models import (
    OFF_ROLL_CALL,
    Class,
    ClassStaff,
    ClassStatus,
    RollCallEntry,
    RollCallSettings,
    RollCallStatus,
    StaffRole,
)
from .schemas import MAX_ROLL_CALL_BATCH, ClassIn, RollCallRefusal, has_control_chars

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


def accounts_with_email(db: Session, email: str) -> list[User]:
    """Every account whose email equals ``email`` ignoring case, in any tenant, deleted and
    deactivated ones included, oldest first: callers decide, never ``.first()`` at random."""
    return db.query(User).filter(func.lower(User.email) == email).order_by(User.created_at, User.id).all()


def is_pre_provisioned(u: User) -> bool:
    """A roster row (CSV import) that no directory identity has claimed yet."""
    return u.source == "csv_import"


# An admin may set an empty allow-list: any domain, in smaller batches. Emails (onboarding
# and the day-before reminder together) are capped per instructor per day either way.
UNRESTRICTED_BATCH = 50
EMAILS_PER_INSTRUCTOR_PER_DAY = 100
NO_DOMAINS = (
    "No email domains are allowed for roll calls yet: an admin must set the allowed email "
    "domains (Classes, roll-call domains)"
)


def allowed_domains(db: Session, tenant_id: uuid.UUID) -> tuple[list[str], str]:
    """(domains, source). An admin's setting if there is one (an empty setting: any
    domain). Otherwise the domains of the tenant's active directory (AD) accounts only: an
    LTI, local or roster address (gmail.com) never widens it. No setting and no AD account:
    nothing is allowed until an admin sets it."""
    # tenant-safe: keyed by the tenant id itself (the class's or the caller's).
    row = db.get(RollCallSettings, tenant_id)
    if row is not None:
        return sorted(row.email_domains or []), "setting"
    emails = (
        db.query(User.email)
        .filter(
            User.tenant_id == tenant_id, User.source == "ad", User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
        .all()
    )
    return sorted({e.rsplit("@", 1)[1].lower() for (e,) in emails if e and "@" in e}), "accounts"


def _account_for(rows: list[User], cls: Class) -> tuple[User | None, str | None, bool]:
    """(account to link, refusal, hidden) for an address's accounts.

    Only this tenant's own facts are ever told to the instructor. An address that names an
    account in another tenant, or several case variants anywhere, is ``hidden``: it is added
    exactly as an unknown address would be (invited, emailed, not linked, not enrolled), so
    the roll call is no oracle for other tenants' accounts. Admission refuses it later
    (onboarding.py), and an audit row lets a platform admin resolve it."""
    if len(rows) > 1 or any(u.tenant_id != cls.tenant_id for u in rows):
        return None, None, True
    if not rows:
        return None, None, False
    u = rows[0]
    if u.deleted_at is not None:
        return None, "belongs to a deleted account", False
    if not u.is_active:
        return None, "belongs to a deactivated account", False
    if u.role != UserRole.student:
        return None, f"belongs to a {u.role.value} account; a roll call holds Students only", False
    return u, None, False


@dataclass
class AddResult:
    added: list[RollCallEntry]
    already: list[str]
    refused: list[RollCallRefusal]
    to_onboard: list[RollCallEntry]
    not_emailed: list[RollCallRefusal] = field(default_factory=list)
    # Added after approval with no room left: waiting for an approver (ADR 0014 §17).
    pending: list[RollCallEntry] = field(default_factory=list)
    # The approved sessions the added Students are now invited to.
    sessions: list = field(default_factory=list)


def add_to_roll_call(db: Session, cls: Class, actor: CurrentUser, emails: list[str]) -> AddResult:
    """Add ``emails`` (already normalised) to the roll call. Does not commit."""
    domains, source = allowed_domains(db, cls.tenant_id)
    if not domains and source == "accounts":
        raise HTTPException(422, NO_DOMAINS)
    cap = MAX_ROLL_CALL_BATCH if domains else UNRESTRICTED_BATCH
    if len(emails) > cap:
        raise HTTPException(422, f"At most {cap} emails at a time")
    res = AddResult(added=[], already=[], refused=[], to_onboard=[])
    hidden: list[str] = []
    for email in emails:
        entry = db.query(RollCallEntry).filter(RollCallEntry.class_id == cls.id, RollCallEntry.email == email).first()
        if entry is not None and entry.status != RollCallStatus.removed.value:
            res.already.append(email)
            continue
        domain = email.rsplit("@", 1)[1]
        if domains and domain not in domains:
            res.refused.append(RollCallRefusal(email=email, reason=f"{domain} is not an email domain this organisation's roll calls accept"))
            continue
        account, refusal, is_hidden = _account_for(accounts_with_email(db, email), cls)
        if refusal:
            res.refused.append(RollCallRefusal(email=email, reason=refusal))
            continue
        if is_hidden:
            hidden.append(email)
        # After approval, one more Student must fit every approved session (ADR 0014 §17).
        # Checked (under the booking lock, when a seat costs anything) before the entry is
        # written, so the lock is the transaction's first write-side step.
        no_room = sessions.room_for_seats(db, cls, 1)
        if entry is None:
            entry = RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email=email)
            db.add(entry)
        entry.added_by = uuid.UUID(actor.id)
        entry.removed_at = None
        entry.user_id = None
        entry.signed_in_at = None
        if no_room:
            entry.status = RollCallStatus.pending.value
            entry.pending_reason = "; ".join(no_room)[:1000]
            res.pending.append(entry)
            continue
        res.sessions = sessions.take_seats(db, cls, 1)
        put_on_roll_call(db, cls, entry, account)
        if entry.status == RollCallStatus.invited.value and entry.onboarding_sent_at is None:
            res.to_onboard.append(entry)
        res.added.append(entry)
    budget = max(0, EMAILS_PER_INSTRUCTOR_PER_DAY - emails_sent_today(db, uuid.UUID(actor.id)))
    for entry in res.to_onboard[budget:]:
        res.not_emailed.append(RollCallRefusal(email=entry.email, reason=DAILY_LIMIT))
    res.to_onboard = res.to_onboard[:budget]
    db.flush()
    if res.added:
        audit(db, actor.id, cls, "roll_call_add", f"added {len(res.added)}: {', '.join(e.email for e in res.added)}"[:2000])
    for entry in res.pending:
        audit(db, actor.id, cls, "roll_call_add_pending", f"{entry.email}: {entry.pending_reason}"[:2000])
    for email in hidden:
        # For the platform admin only: this address names an account elsewhere, or several
        # case variants; it was added unlinked and cannot be admitted. tenant_id is None on
        # purpose: GET /audit-log shows a tenant's admin only rows of their own tenant, and
        # only the platform administrator rows with none, so the row is no oracle for the
        # class's own tenant either. The class's tenant is named in the detail instead.
        db.add(AuditLog(
            user_id=uuid.UUID(actor.id), tenant_id=None, action="roll_call_cross_tenant",
            resource_type="class", resource_id=str(cls.id),
            detail=(f"tenant {cls.tenant_id}: {email} names an account in another tenant or several case "
                    "variants; added unlinked, admission will refuse it until an administrator resolves "
                    "the accounts"),
        ))
    return res


def put_on_roll_call(db: Session, cls: Class, entry: RollCallEntry, account: User | None) -> None:
    """Put ``entry`` on the roll call: linked and enrolled when ``account`` is this tenant's
    Student, signed in when that account has signed in before, otherwise invited. Does not commit."""
    entry.status = RollCallStatus.invited.value
    entry.pending_reason = None
    entry.user_id = None
    entry.signed_in_at = None
    if account is not None:
        entry.user_id = account.id
        ensure_enrollment(db, user_id=account.id, course_id=cls.course_id, tenant_id=cls.tenant_id)
        if not is_pre_provisioned(account):
            entry.status = RollCallStatus.signed_in.value
            entry.signed_in_at = _now()


@dataclass
class AdditionDecision:
    entry: RollCallEntry
    sessions: list
    onboard: bool
    not_emailed: str | None = None


def approve_addition(db: Session, cls: Class, entry: RollCallEntry, actor: CurrentUser) -> AdditionDecision:
    """An approver lets a pending addition onto the roll call (ADR 0014 §17). It must fit the
    approved sessions now (409 with the reasons otherwise), and its address must still be
    one the roll call may take (422). Does not commit."""
    if entry.status != RollCallStatus.pending.value:
        raise HTTPException(409, f"This addition is {entry.status}, not waiting for approval")
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is cancelled; its roll call cannot change")
    # The allow-list may have changed since it was added: the same rule as adding it now.
    domains, source = allowed_domains(db, cls.tenant_id)
    if not domains and source == "accounts":
        raise HTTPException(422, NO_DOMAINS)
    domain = entry.email.rsplit("@", 1)[1]
    if domains and domain not in domains:
        raise HTTPException(422, f"{domain} is not an email domain this organisation's roll calls accept")
    account, refusal, _hidden = _account_for(accounts_with_email(db, entry.email), cls)
    if refusal:
        raise HTTPException(422, f"{entry.email} {refusal}")
    no_room = sessions.room_for_seats(db, cls, 1)
    if no_room:
        raise HTTPException(409, {"message": "It still does not fit the class's approved sessions",
                                  "reasons": no_room})
    taken = sessions.take_seats(db, cls, 1)
    put_on_roll_call(db, cls, entry, account)
    onboard = entry.status == RollCallStatus.invited.value and entry.onboarding_sent_at is None
    not_emailed = None
    # The onboarding email counts against the budget of whoever added them, as any addition's.
    sender = entry.added_by or uuid.UUID(actor.id)
    if onboard and emails_sent_today(db, sender) >= EMAILS_PER_INSTRUCTOR_PER_DAY:
        onboard, not_emailed = False, DAILY_LIMIT
    audit(db, actor.id, cls, "roll_call_addition_approved", entry.email)
    return AdditionDecision(entry, taken, onboard, not_emailed)


def reject_addition(db: Session, cls: Class, entry: RollCallEntry, actor: CurrentUser, reason: str) -> None:
    """An approver turns a pending addition down: it is removed, with the reason. Does not commit."""
    if entry.status != RollCallStatus.pending.value:
        raise HTTPException(409, f"This addition is {entry.status}, not waiting for approval")
    entry.status = RollCallStatus.removed.value
    entry.removed_at = _now()
    entry.pending_reason = f"rejected: {reason}"[:1000]
    audit(db, actor.id, cls, "roll_call_addition_rejected", f"{entry.email}: {reason}"[:2000])


DAILY_LIMIT = (
    f"not emailed: the daily limit of {EMAILS_PER_INSTRUCTOR_PER_DAY} emails per instructor (onboarding and "
    "reminders together) is reached; they can still sign in, and are not reminded beyond the limit"
)


def emails_sent_today(db: Session, added_by: uuid.UUID, now: datetime | None = None) -> int:
    """Onboarding emails and day-before reminders sent today (UTC) for entries this person
    added, in any class: the one budget both draw on."""
    start = datetime.combine((now or _now()).date(), time.min, tzinfo=UTC)
    onboarding = (
        db.query(func.count(RollCallEntry.id))
        .filter(RollCallEntry.added_by == added_by, RollCallEntry.onboarding_sent_at >= start)
        .scalar()
        or 0
    )
    reminders = (
        db.query(func.count(RollCallEntry.id))
        .filter(RollCallEntry.added_by == added_by, RollCallEntry.reminded_at >= start)
        .scalar()
        or 0
    )
    return onboarding + reminders


def remove_from_roll_call(db: Session, cls: Class, entry: RollCallEntry, actor: CurrentUser) -> list:
    """Take a Student off the roll call: always allowed, before or after approval (ADR 0014
    §17). The entry stays, marked removed (ADR 0010). Their account and enrolment are
    untouched: they may be on the course for other reasons. Their seat in each approved
    session is freed; returns those sessions, whose invites they lose. Their own ranges are
    torn down by the class's next range tick (``ranges``), and they leave the teams of
    sessions that have not started. Does not commit."""
    from . import teams

    was_on = entry.status != RollCallStatus.pending.value
    entry.status = RollCallStatus.removed.value
    entry.removed_at = _now()
    teams.drop_member(db, cls, entry.user_id)
    audit(db, actor.id, cls, "roll_call_remove", entry.email)
    return sessions.free_seats(db, cls, 1) if was_on else []


def class_roll_call_emails(db: Session, cls: Class) -> list[str]:
    """Who a class's invites and reminders go to (ADR 0014; #291 review on #278): the roll
    call, not course enrolment.

    An entry linked to an account counts only while that account is an active Student of
    the class's own tenant, with the account's current email. A Student who moved tenant
    is dropped: a class belongs to one tenant, and its mail goes nowhere else. An entry
    with no account yet is the address the lead typed. Removed entries never count, nor do
    additions still waiting for an approver (ADR 0014 §17).

    ``scheduler/invites.recipients`` uses this for a class's sessions (#281,
    ``sessions.roll_call_for_booking``).
    """
    out: set[str] = set()
    rows = (
        db.query(RollCallEntry)
        .filter(
            RollCallEntry.class_id == cls.id,
            RollCallEntry.tenant_id == cls.tenant_id,
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
        )
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


def _subject_safe(value: str) -> str:
    """No header injection: control characters (CR, LF...) become spaces, whatever the
    schema let through before it refused them."""
    return " ".join("".join(" " if has_control_chars(ch) else ch for ch in value).split())[:150]


def onboarding_mail(cls: Class, course_name: str, email: str) -> Mail:
    return Mail(
        to=email,
        subject=f"You are on the roll call for {_subject_safe(cls.name)}",
        body=(
            f"You have been added to the class {cls.name} ({course_name}), {_dates(cls)}.\n\n"
            + _how_to_join()
        ),
    )


def reminder_mail(cls: Class, course_name: str, email: str) -> Mail:
    return Mail(
        to=email,
        subject=f"Reminder: {_subject_safe(cls.name)} starts tomorrow",
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
