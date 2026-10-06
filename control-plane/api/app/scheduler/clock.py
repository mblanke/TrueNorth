"""The scheduler's clock (ADR 0004 §5, slice 4).

Once a minute it moves bookings along their lifecycle as time passes:

1. provisioning lead reached: ``scheduled -> provisioning``, and the booked range is
   built (``provision_range`` on the worker) unless it is already up;
2. session start: ``scheduled | provisioning -> active``;
3. teardown grace after the end: ``active -> completed``, and the range is torn down
   (``destroy_range``) if this booking built it;
4. reminder lead reached: the instructor is emailed once.

The clock runs in the API, where the scheduler's tables and rules live; the work itself
goes to the worker through the contracted tasks, exactly as the ranges router sends it.
Every step is claimed with a guarded update (a lifecycle transition, or
``reminded_at IS NULL``), so any number of API replicas can tick at once and each
step still happens once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import range_lifecycle
from ..models import User
from . import lifecycle, service
from .capacity import PROVISION_LEAD, TEARDOWN_GRACE, window_label
from .models import EventState, ScheduledEvent

logger = logging.getLogger("truenorth.api.scheduler.clock")

S = EventState
TICK_SECONDS = float(os.getenv("SCHEDULER_TICK_SECONDS", "60"))
# 0 turns reminders off.
REMINDER_LEAD = timedelta(minutes=int(os.getenv("SCHEDULER_REMINDER_LEAD_MIN", "1440")))


@dataclass(frozen=True)
class Reminder:
    event_id: str
    to: str
    subject: str
    body: str


@dataclass
class TickResult:
    provisioning: int = 0
    activated: int = 0
    completed: int = 0
    torn_down: int = 0
    reminders: list[Reminder] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "provisioning": self.provisioning,
            "activated": self.activated,
            "completed": self.completed,
            "torn_down": self.torn_down,
            "reminders": len(self.reminders),
        }


def tick(db: Session, now: datetime | None = None) -> TickResult:
    """One pass. Commits after each booking, so one failure does not undo the rest."""
    now = now or datetime.now(UTC)
    res = TickResult()

    # 1. Provisioning lead reached, session not over: build the range.
    for evt in _due(
        db,
        ScheduledEvent.state == S.scheduled,
        ScheduledEvent.range_id.isnot(None),
        ScheduledEvent.start_time <= now + PROVISION_LEAD,
        ScheduledEvent.end_time > now,
    ):
        if _claim(db, evt, S.provisioning):
            db.refresh(evt)  # the range the booking points at now, after any edit that won the row

            def owned(evt=evt):
                evt.auto_provisioned = True

            _, what = range_lifecycle.provision_for_booking(db, evt.range_id, before_build=owned)
            service.audit_system(db, evt, "transition", f"scheduled -> provisioning; {what}")
            db.commit()
            res.provisioning += 1

    # 2. Session started (a booking whose whole window was missed passes through here
    #    to step 3 in the same tick, without building anything).
    for evt in _due(db, ScheduledEvent.state.in_([S.scheduled, S.provisioning]), ScheduledEvent.start_time <= now):
        before = evt.state.value
        if _claim(db, evt, S.active):
            service.audit_system(db, evt, "transition", f"{before} -> active")
            db.commit()
            res.activated += 1

    # 3. Teardown grace after the end: complete, and tear down what we built.
    for evt in _due(db, ScheduledEvent.state == S.active, ScheduledEvent.end_time <= now - TEARDOWN_GRACE):
        if _claim(db, evt, S.completed):
            detail = "active -> completed"
            if what := service.release_range(db, evt):
                detail += f"; {what}"
                res.torn_down += 0 if evt.auto_provisioned else 1
            service.audit_system(db, evt, "transition", detail)
            db.commit()
            res.completed += 1

    # 3b. A finished booking whose range could not be torn down yet (still being built
    #     when the booking was cancelled or ended): retry until it can be.
    for evt in _due(
        db,
        ScheduledEvent.state.in_([S.completed, S.cancelled]),
        ScheduledEvent.auto_provisioned == True,  # noqa: E712
        ScheduledEvent.range_id.isnot(None),
    ):
        what = service.release_range(db, evt)
        if not evt.auto_provisioned:
            service.audit_system(db, evt, "teardown", what or "")
            res.torn_down += 1
        db.commit()

    # 4. Reminders, once per booking, to its instructor.
    if timedelta(0) < REMINDER_LEAD:
        for evt in _due(
            db,
            ScheduledEvent.state.in_([S.scheduled, S.provisioning]),
            ScheduledEvent.reminded_at.is_(None),
            ScheduledEvent.instructor_id.isnot(None),
            ScheduledEvent.start_time > now,
            ScheduledEvent.start_time <= now + REMINDER_LEAD,
        ):
            claimed = (
                db.query(ScheduledEvent)
                .filter(ScheduledEvent.id == evt.id, ScheduledEvent.reminded_at.is_(None))
                .update({ScheduledEvent.reminded_at: now}, synchronize_session=False)
            )
            if claimed != 1:
                db.rollback()
                continue
            teacher = db.get(User, evt.instructor_id)
            if teacher and teacher.email and teacher.is_active and teacher.deleted_at is None:
                res.reminders.append(_reminder(evt, teacher.email))
                service.audit_system(db, evt, "reminder", "instructor emailed")
            db.commit()
    return res


def _due(db: Session, *crit) -> list[ScheduledEvent]:
    return db.query(ScheduledEvent).filter(*crit).order_by(ScheduledEvent.start_time).limit(500).all()


def _claim(db: Session, evt: ScheduledEvent, to: EventState) -> bool:
    """Win the move or step aside: another replica, or a person, got there first."""
    try:
        return lifecycle.transition(db, evt, to)
    except HTTPException:
        return False


def _reminder(evt: ScheduledEvent, to: str) -> Reminder:
    when = window_label(evt.start_time, evt.end_time)
    return Reminder(
        event_id=str(evt.id),
        to=to,
        subject=f"Reminder: {evt.name}, {when}",
        body=(
            f"You are teaching '{evt.name}' {when}.\n\n"
            f"The range is built {int(PROVISION_LEAD.total_seconds() // 60)} minutes before the start "
            f"and torn down {int(TEARDOWN_GRACE.total_seconds() // 60)} minutes after the end.\n"
        ),
    )


async def send_reminders(reminders: list[Reminder]) -> None:
    """Through the existing SMTP channel. At most once: the booking was marked reminded
    before sending, so a failed send is logged, not retried into duplicates."""
    if not reminders:
        return
    from ..notifications import get_channel

    channel = get_channel("email")
    for r in reminders:
        try:
            if not await channel.send(r.to, r.subject, r.body):
                logger.warning("Reminder for booking %s was not delivered", r.event_id)
        except Exception:
            logger.exception("Reminder for booking %s failed", r.event_id)


def _tick_in_own_session() -> TickResult:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        return tick(db)
    finally:
        db.close()


async def run_forever() -> None:
    """Started from the API lifespan. Sleeps first, so a test client that starts the
    app does not tick."""
    logger.info("Scheduler clock started (every %ss)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            result = await asyncio.to_thread(_tick_in_own_session)
            if any(result.summary().values()):
                logger.info("Scheduler tick: %s", result.summary())
            await send_reminders(result.reminders)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed; retrying next tick")
