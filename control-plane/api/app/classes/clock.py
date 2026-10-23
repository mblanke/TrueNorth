"""The day-before reminder (ADR 0014 §2).

Every roll-call Student who has not signed in yet (``invited``) is emailed once, the day
before their class starts. "The day before" is the UTC date: a class has dates, not
times or a time zone, and a tenant has no time zone setting yet. Where the site is far
from UTC the reminder lands up to a day early or late in local terms; per-tenant time
zones would change that, here only. Each entry is claimed with a guarded update
(``reminded_at IS NULL``) before its email is built, so any number of API replicas can tick
and each Student still gets one reminder. Someone added that same day already had their
onboarding email that day and is not reminded on top of it.

Runs in the API beside the scheduler's clock, under the same ``SCHEDULER_CLOCK_ENABLED``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Course, User, UserRole
from . import service
from .models import Class, ClassStatus, RollCallEntry, RollCallStatus

logger = logging.getLogger("truenorth.api.classes.clock")

TICK_SECONDS = float(os.getenv("CLASS_REMINDER_TICK_SECONDS", "900"))


def tick(db: Session, now: datetime | None = None) -> list[service.Mail]:
    """Claim and build the reminders due now. Commits per class."""
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).date()
    out: list[service.Mail] = []
    for cls in _classes_starting(db, tomorrow):
        # tenant-safe: the class's own course, which was checked against its tenant when set.
        course = db.get(Course, cls.course_id)
        course_name = course.name if course else "the course"
        due = (
            db.query(RollCallEntry)
            .filter(
                RollCallEntry.class_id == cls.id,
                RollCallEntry.status == RollCallStatus.invited.value,
                RollCallEntry.reminded_at.is_(None),
            )
            .all()
        )
        sent = 0
        held = 0
        for entry in due:
            if entry.onboarding_sent_at is not None and entry.onboarding_sent_at.date() == now.date():
                continue
            if _has_signed_in(db, cls, entry):
                # Linked to an account that has signed in (adopted another way): in effect
                # signed in (forfeit._signed_in_since), so no "you have not signed in" reminder.
                continue
            # Reminders draw on the same per-instructor daily budget as onboarding emails
            # (whoever added the entry), so a roll call over the cap is not mailed by the clock
            # instead. reminded_at counts once claimed, so the count stays exact as we go.
            if entry.added_by is not None and (
                service.emails_sent_today(db, entry.added_by, now) >= service.EMAILS_PER_INSTRUCTOR_PER_DAY
            ):
                held += 1
                continue
            claimed = (
                db.query(RollCallEntry)
                .filter(RollCallEntry.id == entry.id, RollCallEntry.reminded_at.is_(None))
                .update({RollCallEntry.reminded_at: now}, synchronize_session=False)
            )
            if claimed == 1:
                out.append(service.reminder_mail(cls, course_name, entry.email))
                sent += 1
        if sent or held:
            service.audit(
                db, None, cls, "roll_call_reminder",
                f"reminded {sent} Student(s) who have not signed in; {held} held by the daily email limit",
            )
        db.commit()
    return out


PAGE = 500


def _has_signed_in(db: Session, cls: Class, entry: RollCallEntry) -> bool:
    """The entry's linked account is an active Student of the class's tenant that is no
    longer a roster row: it has signed in, though the entry still says invited."""
    if entry.user_id is None:
        return False
    # tenant-safe: the entry's own link, checked against the class's tenant on the next line.
    u = db.get(User, entry.user_id)
    return (
        u is not None
        and u.tenant_id == cls.tenant_id
        and u.is_active
        and u.deleted_at is None
        and u.role == UserRole.student
        and not service.is_pre_provisioned(u)
    )


def _classes_starting(db: Session, day):
    """Every open class starting on ``day``, a page at a time (keyset on id), so a large
    site is never cut off at the first page."""
    last = None
    while True:
        q = db.query(Class).filter(Class.status == ClassStatus.open.value, Class.start_date == day)
        if last is not None:
            q = q.filter(Class.id > last)
        page = q.order_by(Class.id).limit(PAGE).all()
        yield from page
        if len(page) < PAGE:
            return
        last = page[-1].id


def _tick_in_own_session() -> list[service.Mail]:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        return tick(db)
    finally:
        db.close()


async def run_forever() -> None:
    """Started from the API lifespan. Sleeps first, so a test client does not tick."""
    logger.info("Class reminder clock started (every %ss)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            mails = await asyncio.to_thread(_tick_in_own_session)
            if mails:
                logger.info("Class reminders: %d", len(mails))
            await service.send(mails)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Class reminder tick failed; retrying next tick")
