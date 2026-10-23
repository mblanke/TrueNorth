"""Seat forfeit and reinstatement (ADR 0014 §4; issue #280).

**The rule.** A Student on a class's roll call who has not signed in by 2 hours after the
class's *first approved session* starts loses their seat. The 2 hours are fixed for now
(ADR 0014, Assumptions).

- *The start* is the start of the earliest class session an approver let through
  (``scheduler.service.first_approved_start``): approved and holding, or already over.
  Sessions awaiting approval, rejected or cancelled never count. No approved session: no
  start, so nobody forfeits.
- *Signed in* means the entry reached ``signed_in`` or later. An entry gets there when:
  the Student's first AD sign-in admits them from the roll call
  (``onboarding.admit_from_roll_call``); they were added already holding an account that
  has signed in (``service.put_on_roll_call``); or, for an entry linked to an existing
  account but still ``invited`` (a roster row adopted some other way, such as Request
  access), when that account next authenticates (``note_sign_in``, from ``GET /auth/me``),
  before or after the class starts. Only ``invited`` entries can forfeit.
- *Late additions.* Someone put on the roll call after the start gets their own 2 hours,
  from when they were added (``joined_at``).
- *Once.* A reinstated entry keeps ``forfeited_at`` and is never forfeited again.

**Forfeiting** (``tick``, the API's class clock, every ``CLASS_FORFEIT_TICK_SECONDS``):

- each entry is claimed with a guarded update (``status = invited AND forfeited_at IS
  NULL``), so any number of API replicas can tick at once, and a sign-in racing the clock
  either admits the Student or loses to the forfeit, never both;
- only the entries this replica won free seats: one ``sessions.free_seats`` per class for
  all of them, which raises each approved session's SEQUENCE, and each forfeited Student
  is sent a CANCEL of the sessions still to come;
- an audit row per class names who forfeited;
- the class's lead and assistant instructors get one email per class per tick.

That email does **not** draw on the per-instructor daily email budget
(``service.EMAILS_PER_INSTRUCTOR_PER_DAY``). The budget caps mail an instructor causes to be
sent to addresses they typed in (onboarding and reminders to Students): an abuse limit on
outbound mail. This notice is system mail to the class's own staff accounts, one per class
per tick that forfeited anyone, so it is bounded by the number of classes. Charging it to the
budget would let a large no-show silence that instructor's onboarding and reminders that
day, or drop the notice ADR 0014 §4 requires.

Forfeiting touches this roll-call entry only: never the account, the course enrolment, or
the Student's entries on other classes.

**Reinstating** (``reinstate``): the class's lead or an admin, one click. The Student goes
back to ``signed_in`` if their account has signed in since (or now names one that has),
otherwise to ``invited``; their seat is taken again with ADR 0014 §17's rule, automatically
if it fits every approved session, otherwise as an addition waiting for an approver.
Audited.

**Signing in after forfeiting.** Admission matches ``invited`` entries only, so a forfeited
entry never admits anyone; ``forfeited_class_names`` lets ``GET /auth/me`` tell the Student
to ask their instructor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, User, UserRole
from ..scheduler import service as scheduler
from . import service, sessions
from .models import Class, ClassStaff, ClassStatus, RollCallEntry, RollCallStatus, StaffRole

logger = logging.getLogger("truenorth.api.classes.forfeit")

FORFEIT_AFTER = timedelta(hours=2)  # ADR 0014 §4; fixed for now (Assumptions)
TICK_SECONDS = float(os.getenv("CLASS_FORFEIT_TICK_SECONDS", "60"))
PAGE = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def class_start(db: Session, cls: Class) -> datetime | None:
    """When the class's first approved session starts; None when none is approved."""
    return scheduler.first_approved_start(db, sessions.session_ids(db, cls))


def deadline(start: datetime, entry: RollCallEntry) -> datetime:
    """When ``entry`` forfeits, given the class's start: 2 hours after the later of the start
    and when the entry was put on the roll call."""
    joined = _aware(entry.joined_at) if entry.joined_at is not None else None
    return max(start, joined or start) + FORFEIT_AFTER


@dataclass
class Forfeited:
    """One class's forfeits from one tick, won by this replica. Plain values: they outlive
    the session."""

    class_id: uuid.UUID
    emails: list[str]
    sessions: list = field(default_factory=list)  # the approved sessions their seats left


@dataclass
class TickResult:
    forfeited: list[Forfeited] = field(default_factory=list)
    mails: list[service.Mail] = field(default_factory=list)
    invites: list = field(default_factory=list)


def tick(db: Session, now: datetime | None = None) -> TickResult:
    """Forfeit every seat due now. Commits per class, so one failure undoes nothing else."""
    now = now or _now()
    res = TickResult()
    for cls in _classes_with_invited(db, now):
        try:
            done = forfeit_due(db, cls, now)
            if done is None:
                continue
            res.invites += scheduler.attendee_invites(db, done.sessions, done.emails, "CANCEL")
            res.mails += notice_mails(db, cls, done.emails)
            db.commit()
            res.forfeited.append(done)
        except Exception:
            db.rollback()
            logger.exception("Seat forfeit for class %s failed; retrying next tick", cls.id)
    return res


def _classes_with_invited(db: Session, now: datetime):
    """Open classes that have not ended and still have someone ``invited`` who never
    forfeited, a page at a time (keyset on id)."""
    yesterday = (now - timedelta(days=1)).date()  # a class's dates are UTC dates; one day of slack
    last = None
    while True:
        has_invited = (
            db.query(RollCallEntry.id)
            .filter(
                RollCallEntry.class_id == Class.id,
                RollCallEntry.tenant_id == Class.tenant_id,
                RollCallEntry.status == RollCallStatus.invited.value,
                RollCallEntry.forfeited_at.is_(None),
            )
            .exists()
        )
        q = db.query(Class).filter(Class.status == ClassStatus.open.value, Class.end_date >= yesterday, has_invited)
        if last is not None:
            q = q.filter(Class.id > last)
        page = q.order_by(Class.id).limit(PAGE).all()
        yield from page
        if len(page) < PAGE:
            return
        last = page[-1].id


def forfeit_due(db: Session, cls: Class, now: datetime) -> Forfeited | None:
    """Claim and forfeit the entries of ``cls`` that are due at ``now``. Does not commit.
    None when nothing was won (nothing due, or another replica got there first)."""
    start = class_start(db, cls)
    if start is None or now < start + FORFEIT_AFTER:
        return None
    due = [
        e
        for e in db.query(RollCallEntry).filter(
            RollCallEntry.class_id == cls.id,
            RollCallEntry.tenant_id == cls.tenant_id,
            RollCallEntry.status == RollCallStatus.invited.value,
            RollCallEntry.forfeited_at.is_(None),
        )
        if now >= deadline(start, e)
    ]
    won = [e for e in due if _claim(db, e, now)]
    if not won:
        return None
    for e in won:
        db.refresh(e)
    freed = sessions.free_seats(db, cls, len(won))
    service.audit(
        db, None, cls, "roll_call_forfeit",
        (f"{len(won)} Student(s) not signed in by {_hhmm(start + FORFEIT_AFTER)} (2 hours after the first "
         f"approved session started) lost their seat: {', '.join(e.email for e in won)}")[:2000],
    )
    return Forfeited(class_id=cls.id, emails=[e.email for e in won], sessions=freed)


def _claim(db: Session, entry: RollCallEntry, now: datetime) -> bool:
    """Win the forfeit or step aside: another replica forfeited it, or the Student signed in."""
    return (
        db.query(RollCallEntry)
        .filter(
            RollCallEntry.id == entry.id,
            RollCallEntry.status == RollCallStatus.invited.value,
            RollCallEntry.forfeited_at.is_(None),
        )
        .update(
            {RollCallEntry.status: RollCallStatus.forfeited.value, RollCallEntry.forfeited_at: now},
            synchronize_session=False,
        )
        == 1
    )


def _hhmm(t: datetime) -> str:
    return f"{_aware(t):%a %d %b %Y %H:%M} UTC"


# ── Who is told ────────────────────────────────────────────────────────────


def instructors(db: Session, cls: Class) -> list[User]:
    """The class's lead and assistant instructors who can still receive mail: active,
    undeleted accounts of the class's own tenant."""
    assistants = db.query(ClassStaff.user_id).filter(
        ClassStaff.class_id == cls.id, ClassStaff.role == StaffRole.assistant.value
    )
    return (
        db.query(User)
        .filter(
            or_(User.id == cls.lead_instructor_id, User.id.in_(assistants)),
            User.tenant_id == cls.tenant_id,
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
            User.role.in_((UserRole.instructor, UserRole.admin)),
        )
        .order_by(User.email)
        .all()
    )


def notice_mails(db: Session, cls: Class, emails: list[str]) -> list[service.Mail]:
    """One email per instructor: who lost their seat on this class, and how to reinstate them."""
    url = service.sign_in_url()
    where = f" ({url}/classes)" if url else ""
    names = "\n".join(f"  - {e}" for e in emails)
    body = (
        f"{len(emails)} Student(s) on the roll call of {cls.name} had not signed in to TrueNorth Range by 2 hours "
        f"after the class's first session started, and lost their seat:\n\n{names}\n\n"
        "Their seat in the class's sessions was freed and their calendar invites withdrawn. Nothing else changed: "
        "their account, their enrolment and their other classes are as they were.\n\n"
        f"To give someone their seat back, open the class's roll call on the Classes page{where} and choose "
        "Reinstate.\n"
    )
    subject = f"{service._subject_safe(cls.name)}: {len(emails)} Student(s) lost their seat"
    return [service.Mail(to=u.email, subject=subject, body=body) for u in instructors(db, cls) if u.email]


# ── Signing in ─────────────────────────────────────────────────────────────


def note_sign_in(db: Session, user: User) -> int:
    """``user`` has just authenticated (``GET /auth/me``): every entry of an open class still
    ``invited`` and linked to them becomes ``signed_in``. Guarded on ``invited``, so a
    forfeited entry stays forfeited. Only an active Student of the entry's own tenant counts.
    Commits when it changes anything; returns how many entries it signed in."""
    if user.role != UserRole.student or not user.is_active or user.deleted_at is not None:
        return 0
    rows = (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            RollCallEntry.user_id == user.id,
            RollCallEntry.tenant_id == user.tenant_id,
            Class.tenant_id == user.tenant_id,
            Class.status == ClassStatus.open.value,
            RollCallEntry.status == RollCallStatus.invited.value,
        )
        .all()
    )
    if not rows:
        return 0
    now = _now()
    n = 0
    for entry, cls in rows:
        claimed = (
            db.query(RollCallEntry)
            .filter(RollCallEntry.id == entry.id, RollCallEntry.status == RollCallStatus.invited.value)
            .update(
                {RollCallEntry.status: RollCallStatus.signed_in.value, RollCallEntry.signed_in_at: now},
                synchronize_session=False,
            )
        )
        if claimed == 1:
            n += 1
            db.add(AuditLog(
                user_id=user.id, tenant_id=cls.tenant_id, action="roll_call_signed_in",
                resource_type="class", resource_id=str(cls.id),
                detail=f"roll-call entry {entry.id}: its linked account signed in",
            ))
    db.commit()
    return n


def forfeited_class_names(
    db: Session, *, email: str | None = None, user_id: uuid.UUID | None = None, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """Open, unfinished classes whose seat this person lost, by their verified email or their
    account, for the "ask your instructor" message. Only the class names, which their own
    onboarding email already told them. Someone with an account (``tenant_id``) hears only of
    their own tenant's classes."""
    crit = []
    if email:
        crit.append(RollCallEntry.email == email)
    if user_id is not None:
        crit.append(RollCallEntry.user_id == user_id)
    if not crit:
        return []
    today = _now().date()
    rows = (
        db.query(Class.name)
        .join(RollCallEntry, RollCallEntry.class_id == Class.id)
        .filter(
            or_(*crit),
            RollCallEntry.status == RollCallStatus.forfeited.value,
            RollCallEntry.tenant_id == Class.tenant_id,
            Class.status == ClassStatus.open.value,
            Class.end_date >= today,
            *([Class.tenant_id == tenant_id] if tenant_id is not None else []),
        )
        .order_by(Class.start_date, Class.name)
        .all()
    )
    return list(dict.fromkeys(name for (name,) in rows))


# ── Reinstating ────────────────────────────────────────────────────────────


@dataclass
class Reinstated:
    entry: RollCallEntry
    sessions: list  # the approved sessions they are invited to again (none while pending)


def reinstate(db: Session, cls: Class, entry: RollCallEntry, actor: CurrentUser) -> Reinstated:
    """Give a forfeited Student their seat back (the caller checked lead or admin). Does not
    commit; the caller sends the returned sessions' invites after it does."""
    if entry.status != RollCallStatus.forfeited.value:
        raise HTTPException(409, f"This Student is {entry.status}; only one who lost their seat is reinstated")
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is cancelled; its roll call cannot change")
    account, refusal, _hidden = service._account_for(service.accounts_with_email(db, entry.email), cls)
    if refusal:
        raise HTTPException(422, f"{entry.email} {refusal}")
    no_room = sessions.room_for_seats(db, cls, 1)
    if no_room:
        # ADR 0014 §17: an addition that does not fit goes to the approver. forfeited_at stays,
        # so once approved the clock leaves them alone.
        entry.status = RollCallStatus.pending.value
        entry.pending_reason = ("reinstated after losing their seat; " + "; ".join(no_room))[:1000]
        entry.added_by = entry.added_by or uuid.UUID(actor.id)
        service.audit(db, actor.id, cls, "roll_call_reinstate_pending", f"{entry.email}: {entry.pending_reason}"[:2000])
        return Reinstated(entry, [])
    taken = sessions.take_seats(db, cls, 1)
    service.put_on_roll_call(db, cls, entry, account)
    service.audit(db, actor.id, cls, "roll_call_reinstate", f"{entry.email} -> {entry.status}")
    return Reinstated(entry, taken)


# ── The loop ───────────────────────────────────────────────────────────────


def _tick_in_own_session() -> TickResult:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        return tick(db)
    finally:
        db.close()


async def run_forever() -> None:
    """Started from the API lifespan beside the reminder clock. Sleeps first, so a test
    client does not tick."""
    logger.info("Class seat-forfeit clock started (every %ss)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            result = await asyncio.to_thread(_tick_in_own_session)
            if result.forfeited:
                logger.info("Seats forfeited: %d", sum(len(f.emails) for f in result.forfeited))
            await service.send(result.mails)
            await scheduler.send_invites(result.invites)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Class seat-forfeit tick failed; retrying next tick")
