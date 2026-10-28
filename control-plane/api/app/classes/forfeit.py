"""Seat forfeit and reinstatement (ADR 0014 §4; issue #280).

**The rule.** A Student on a class's roll call who has not signed in by 2 hours after the
class's *first approved session* starts loses their seat. The 2 hours are fixed for now
(ADR 0014, Assumptions).

- *The start* is the start of the earliest class session an approver let through
  (``scheduler.service.approved_windows``): approved and holding, or already over.
  Sessions awaiting approval, rejected or cancelled never count. No approved session: no
  start, so nobody forfeits. A session approved after it started counts from the approval.
- *Signed in* means the entry reached ``signed_in`` or later. An entry gets there when:
  the Student's first AD sign-in admits them from the roll call
  (``onboarding.admit_from_roll_call``); they were added already holding an account that
  has signed in (``service.put_on_roll_call``); or, for an entry linked to an existing
  account but still ``invited`` (a roster row adopted some other way, such as Request
  access), when that account next authenticates (``note_sign_in``, from ``GET /auth/me``),
  before or after the class starts. The clock itself also counts such an entry as signed in
  when its account has signed in (``service.has_signed_in``, the same test
  ``put_on_roll_call`` uses), so an entry adopted before this rule existed, or by a Student
  whose open tab never calls ``/auth/me`` again, is not forfeited. Only ``invited`` entries
  can forfeit.
- *Late additions.* Someone put on the roll call after the start counts from the first
  approved session not over when they were added (``joined_at``): from when they were added
  if that is during it, from its start if it is later (added in the evening: the next
  morning's session). ``deadline`` holds the whole rule.
- *Never told, never forfeited* (owner direction, #319). Only a Student who was actually
  notified loses a seat: an onboarding or day-before email the SMTP relay accepted
  (``notified_at``, recorded by ``service.deliver`` after the send), or an in-app notification
  issued to their linked account (``in_app_notified_at``) that they have signed in since
  (``ad_status_sync.last_seen``). With email off, a refused or failed send, or the daily email
  limit, the seat is kept, and the roll call shows the lead "not notified, seat kept".
- *Signed in by email.* An unlinked entry whose address names exactly one account, an
  active Student of the class's tenant made by a directory sign-in (``source = ad``:
  Request access, ambiguous or failed admission, adoption), counts as signed in, and is
  linked and enrolled: on that account's next ``GET /auth/me``, or by the clock
  (``counting_account``). An address an LTI platform asserted never counts.
- *Once.* A reinstated entry keeps ``forfeited_at`` and is never forfeited again.

**Forfeiting** (``tick``, the API's class clock, every ``CLASS_FORFEIT_TICK_SECONDS``):

- each entry is claimed with a guarded update (``status = invited AND forfeited_at IS
  NULL``), so any number of API replicas can tick at once, and a sign-in racing the clock
  either admits the Student or loses to the forfeit, never both;
- only the entries this replica won free seats: one ``sessions.free_seats`` per class for
  all of them, which raises each approved session's SEQUENCE, and each forfeited Student
  is sent a CANCEL of the sessions still to come;
- an audit row per class names who forfeited;
- the class's lead and assistant instructors get one email per class per tick (the
  tenant's admins when the class has no active lead or assistant left);
- the CANCELs and emails are handed back only once the class's commit succeeded.

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
Each step is guarded: the entry is claimed with ``status = forfeited`` before a seat is
taken, so two clicks (or the lead and an admin together) take one seat, and a removal
racing it is refused (``service.remove_from_roll_call``). The address must still be one the
roll call accepts, as for an approver's decision. Audited.

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
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..enrollment import ensure_enrollment
from ..models import AuditLog, User, UserRole
from ..scheduler import service as scheduler
from . import service, sessions
from .models import Class, ClassStaff, ClassStatus, RollCallEntry, RollCallStatus, StaffRole

logger = logging.getLogger("truenorth.api.classes.forfeit")

FORFEIT_AFTER = timedelta(hours=2)  # ADR 0014 §4; fixed for now (Assumptions)
TICK_SECONDS = float(os.getenv("CLASS_FORFEIT_TICK_SECONDS", "60"))
PAGE = 500
# At most this many forfeit notices per class per day (one per tick that forfeited anyone).
NOTICES_PER_CLASS_PER_DAY = 24


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def class_windows(db: Session, cls: Class) -> list:
    """The class's approved sessions, earliest first (``scheduler.service.ApprovedWindow``)."""
    return scheduler.approved_windows(db, sessions.session_ids(db, cls))


def deadline(windows: list, entry: RollCallEntry) -> datetime | None:
    """When ``entry`` forfeits; None while nothing can start its clock.

    The clock starts at the first approved session that has not ended by the time the entry
    was put on the roll call (``joined_at``; an entry older than the rule: the class's first
    approved session), at the latest of: that session's start, when an approver let it
    through (a session approved after it started gives its 2 hours from the approval), and
    when the entry joined (someone added during a session gets their own 2 hours; someone
    added in the evening, from the next session's start)."""
    joined = _aware(entry.joined_at) if entry.joined_at is not None else None
    for w in windows:
        if joined is not None and w.end <= joined:
            continue
        anchor = max(w.start, w.approved_at or w.start, joined or w.start)
        return anchor + FORFEIT_AFTER
    return None


def notified(entry: RollCallEntry, seen: dict) -> bool:
    """Whether the Student was actually told to sign in (owner direction, #319): an onboarding
    or day-before email the SMTP relay accepted (``notified_at``), or an in-app notification
    issued to their linked account that they have signed in since (``seen``: user id -> last
    seen, ``last_seen.last_seen``). An email claimed but never sent (email off, a refused or
    failed send, the daily limit) tells nobody, so it never costs a seat."""
    if entry.notified_at is not None:
        return True
    if entry.in_app_notified_at is None or entry.user_id is None:
        return False
    at = seen.get(entry.user_id)
    return at is not None and at >= _aware(entry.in_app_notified_at)


def seen_for(db: Session, entries: list[RollCallEntry]) -> dict:
    """When the linked accounts of ``entries`` were last seen, for ``notified``."""
    from ..ad_status_sync.last_seen import last_seen

    return last_seen(db, [e.user_id for e in entries if e.in_app_notified_at is not None and e.notified_at is None])


def kept_not_notified(windows: list, entry: RollCallEntry, seen: dict, now: datetime) -> bool:
    """Past their deadline and still invited, but never notified: the seat is kept (#319)."""
    if entry.status != RollCallStatus.invited.value or entry.forfeited_at is not None:
        return False
    when = deadline(windows, entry)
    return when is not None and now >= when and not notified(entry, seen)


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
                db.commit()  # an entry the clock found signed in (_signed_in_since) is kept
                continue
            invites = scheduler.attendee_invites(db, done.sessions, done.emails, "CANCEL")
            # One notice per tick that forfeited anyone, at most NOTICES_PER_CLASS_PER_DAY a day.
            db.flush()  # this round's audit row counts
            mails = notice_mails(db, cls, done.emails) if _notices_today(db, cls) <= NOTICES_PER_CLASS_PER_DAY else []
            db.commit()
            # Only now: a commit that failed sends no CANCEL and no notice for a seat still held.
            res.invites += invites
            res.mails += mails
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
    windows = class_windows(db, cls)
    if not windows:
        return None
    due = []
    invited = db.query(RollCallEntry).filter(
        RollCallEntry.class_id == cls.id,
        RollCallEntry.tenant_id == cls.tenant_id,
        RollCallEntry.status == RollCallStatus.invited.value,
        RollCallEntry.forfeited_at.is_(None),
    ).all()
    seen = seen_for(db, invited)
    for e in invited:
        when = deadline(windows, e)
        if when is not None and now >= when and notified(e, seen):
            due.append((e, when))
    due = [(e, when) for e, when in due if not _signed_in_since(db, cls, e, now)]
    won = [(e, when) for e, when in due if _claim(db, e, now)]
    if not won:
        return None
    from . import teams

    for e, _ in won:
        db.refresh(e)
        teams.drop_member(db, cls, e.user_id)  # off the teams of sessions not started yet (#282)
    # Sized again from the roll call, under the booking lock (sessions._reseat takes it).
    freed = sessions.free_seats(db, cls, len(won))
    service.audit(
        db, None, cls, "roll_call_forfeit",
        ("lost their seat, not signed in 2 hours after their class's approved session started: "
         + ", ".join(f"{e.email} (by {_hhmm(when)})" for e, when in won))[:2000],
    )
    return Forfeited(class_id=cls.id, emails=[e.email for e, _ in won], sessions=freed)


def counting_account(db: Session, cls: Class, entry: RollCallEntry) -> User | None:
    """The account whose sign-in counts for ``entry``, if it has signed in; else None.

    - Linked (``user_id``): that account, when it is an active Student of the class's tenant
      that has signed in (``service.has_signed_in``: no roster or admin-made row).
    - Not linked: the one account with the entry's address (ignoring case, in any tenant),
      when it is an active Student of the class's tenant made by a directory sign-in
      (``source = ad``: Request access, roll-call admission, adoption), and whose address
      is the entry's exactly, in ASCII. An address an LTI platform asserted (``lti``) or an
      admin typed (``local``) is not proof of anything."""
    if entry.user_id is not None:
        # tenant-safe: the entry's own link, checked against the class's tenant below.
        u = db.get(User, entry.user_id)
        if u is None or not service.has_signed_in(u):
            return None
    else:
        rows = service.accounts_with_email(db, entry.email)
        if len(rows) != 1 or rows[0].source != "ad":
            return None
        u = rows[0]
        # Exactly the entry's address, in ASCII: lower() folds look-alikes such as the Kelvin
        # sign (U+212A) onto ASCII letters, in PostgreSQL and Python alike, so "\u212aate@corp.test"
        # would otherwise claim kate@corp.test's seat (admission has the same rule).
        if not (u.email or "").isascii() or service.normalise_email(u.email) != entry.email:
            return None
    if u.tenant_id != cls.tenant_id or not u.is_active or u.deleted_at is not None or u.role != UserRole.student:
        return None
    return u


def sign_in_entry(db: Session, cls: Class, entry: RollCallEntry, user: User, now: datetime, why: str) -> bool:
    """Mark ``entry`` signed in for ``user``: linked, enrolled on the class's course, audited.
    Guarded: only while it is ``invited`` and unlinked or linked to ``user``."""
    claimed = (
        db.query(RollCallEntry)
        .filter(
            RollCallEntry.id == entry.id,
            RollCallEntry.status == RollCallStatus.invited.value,
            or_(RollCallEntry.user_id.is_(None), RollCallEntry.user_id == user.id),
        )
        .update(
            {
                RollCallEntry.status: RollCallStatus.signed_in.value,
                RollCallEntry.user_id: user.id,
                RollCallEntry.signed_in_at: now,
            },
            synchronize_session=False,
        )
    )
    if claimed != 1:
        return False
    ensure_enrollment(db, user_id=user.id, course_id=cls.course_id, tenant_id=cls.tenant_id)
    db.add(AuditLog(
        user_id=user.id, tenant_id=cls.tenant_id, action="roll_call_signed_in", resource_type="class",
        resource_id=str(cls.id), detail=f"roll-call entry {entry.id}: {why}"[:2000],
    ))
    return True


def _signed_in_since(db: Session, cls: Class, entry: RollCallEntry, now: datetime) -> bool:
    """The entry's account has signed in although the entry still says invited (an account
    made through Request access, adopted, or made before this rule): it becomes
    ``signed_in`` here and is not forfeited."""
    u = counting_account(db, cls, entry)
    if u is None:
        return False
    sign_in_entry(db, cls, entry, u, now, "its account has signed in (found by the forfeit clock)")
    return True


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


def _notices_today(db: Session, cls: Class) -> int:
    """Forfeit rounds audited for this class since midnight (UTC), this one included."""
    start = datetime.combine(_now().date(), datetime.min.time(), tzinfo=UTC)
    return (
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.resource_type == "class", AuditLog.resource_id == str(cls.id),
            AuditLog.action == "roll_call_forfeit", AuditLog.timestamp >= start,
        )
        .scalar()
        or 0
    )


def tenant_admins(db: Session, cls: Class) -> list[User]:
    """The class's tenant's active admins: told instead when the class has no active lead or
    assistant left, so a forfeit never goes unannounced."""
    return (
        db.query(User)
        .filter(
            User.tenant_id == cls.tenant_id, User.role == UserRole.admin, User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
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
        f"after the class's session started, and lost their seat:\n\n{names}\n\n"
        "Their seat in the class's sessions was freed and their calendar invites withdrawn. Nothing else changed: "
        "their account, their enrolment and their other classes are as they were.\n\n"
        f"To give someone their seat back, open the class's roll call on the Classes page{where} and choose "
        "Reinstate.\n"
    )
    subject = f"{service._subject_safe(cls.name)}: {len(emails)} Student(s) lost their seat"
    to = instructors(db, cls) or tenant_admins(db, cls)
    return [service.Mail(to=u.email, subject=subject, body=body) for u in to if u.email]


# ── Signing in ─────────────────────────────────────────────────────────────


def note_sign_in(db: Session, user: User, *, directory: bool = True) -> int:
    """``user`` has just authenticated (``GET /auth/me``): every entry of an open class of
    their tenant that is still ``invited`` and is theirs becomes ``signed_in`` (linked and
    enrolled): entries linked to them, and, for a directory sign-in (``directory``; not an
    LTI hand-off), unlinked entries with their address when ``counting_account`` says it is
    theirs. A forfeited entry stays forfeited. Commits when it changes anything; returns how
    many entries it signed in."""
    if user.role != UserRole.student or not user.is_active or user.deleted_at is not None:
        return 0
    mine = [RollCallEntry.user_id == user.id]
    if directory and user.source == "ad" and user.email:
        mine.append(and_(RollCallEntry.user_id.is_(None), RollCallEntry.email == service.normalise_email(user.email)))
    rows = (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            or_(*mine),
            RollCallEntry.tenant_id == user.tenant_id,
            Class.tenant_id == user.tenant_id,
            Class.status == ClassStatus.open.value,
            RollCallEntry.status == RollCallStatus.invited.value,
        )
        .all()
    )
    now = _now()
    n = 0
    for entry, cls in rows:
        if entry.user_id is None:
            counted = counting_account(db, cls, entry)
            if counted is None or counted.id != user.id:
                continue
        if sign_in_entry(db, cls, entry, user, now, "its account signed in"):
            n += 1
    if n:
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
    service.assert_domain_allowed(db, cls, entry.email)
    account, refusal, _hidden = service._account_for(service.accounts_with_email(db, entry.email), cls)
    if refusal:
        raise HTTPException(422, f"{entry.email} {refusal}")
    # The booking lock first (when a seat costs anything), as every roll-call addition does.
    no_room = sessions.room_for_seats(db, cls, 1)
    # Then claim the entry: two clicks, or the lead and an admin at once, reinstate it once.
    claimed = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.id == entry.id, RollCallEntry.status == RollCallStatus.forfeited.value)
        .update({RollCallEntry.status: RollCallStatus.pending.value}, synchronize_session=False)
    )
    if claimed != 1:
        raise HTTPException(409, "This Student's place on the roll call changed at the same moment; reload")
    db.refresh(entry)
    if no_room:
        # ADR 0014 §17: an addition that does not fit goes to the approver. forfeited_at stays,
        # so once approved the clock leaves them alone.
        entry.status = RollCallStatus.pending.value
        entry.pending_reason = ("reinstated after losing their seat; " + "; ".join(no_room))[:1000]
        entry.added_by = entry.added_by or uuid.UUID(actor.id)
        service.audit(db, actor.id, cls, "roll_call_reinstate_pending", f"{entry.email}: {entry.pending_reason}"[:2000])
        return Reinstated(entry, [])
    service.put_on_roll_call(db, cls, entry, account)
    taken = sessions.take_seats(db, cls, 1)  # sized from the roll call, with them back on it
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
            await service.send(result.mails)  # staff notices: nothing to record
            await scheduler.send_invites(result.invites)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Class seat-forfeit tick failed; retrying next tick")
