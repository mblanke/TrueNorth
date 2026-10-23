"""The day-before reminder (ADR 0014 §2).

Every roll-call Student who has not signed in yet (``invited``) is emailed once, the day
before their class starts (in UTC). Each entry is claimed with a guarded update
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

from ..models import Course
from . import service
from .models import Class, ClassStatus, RollCallEntry, RollCallStatus

logger = logging.getLogger("truenorth.api.classes.clock")

TICK_SECONDS = float(os.getenv("CLASS_REMINDER_TICK_SECONDS", "900"))


def tick(db: Session, now: datetime | None = None) -> list[service.Mail]:
    """Claim and build the reminders due now. Commits per class."""
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).date()
    out: list[service.Mail] = []
    classes = (
        db.query(Class)
        .filter(Class.status == ClassStatus.open.value, Class.start_date == tomorrow)
        .order_by(Class.id)
        .limit(500)
        .all()
    )
    for cls in classes:
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
        for entry in due:
            if entry.onboarding_sent_at is not None and entry.onboarding_sent_at.date() == now.date():
                continue
            claimed = (
                db.query(RollCallEntry)
                .filter(RollCallEntry.id == entry.id, RollCallEntry.reminded_at.is_(None))
                .update({RollCallEntry.reminded_at: now}, synchronize_session=False)
            )
            if claimed == 1:
                out.append(service.reminder_mail(cls, course_name, entry.email))
                sent += 1
        if sent:
            service.audit(db, None, cls, "roll_call_reminder", f"reminded {sent} Student(s) who have not signed in")
        db.commit()
    return out


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
