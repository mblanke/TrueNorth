"""The scheduler's clock (ADR 0004 §5, slice 4).

Once a minute it moves bookings along their lifecycle as time passes:

1. provisioning lead reached: ``scheduled -> provisioning``, and the booked range is
   built (``provision_range`` on the worker) unless it is already up. Not while a
   maintenance window covers the range (ADR 0013 §7): the booking stays ``scheduled``,
   past its start too, and is built on the first tick after the window ends if the
   session has not ended (1b catches a booking that went ``active`` with its range never
   built);
2. session start: ``scheduled | provisioning -> active``;
3. teardown grace after the end: ``active -> completed``, and the range is torn down
   (``destroy_range``) if this booking built it, inside a maintenance window too;
4. reminder lead reached: the instructor is emailed once.

A class session awaiting approval (``pending_approval``, ADR 0014 §15) is never touched by
steps 0-4: nothing is built, activated or reminded until an approver approves it. One whose
session ends before anyone decides is cancelled (step 3c), so it leaves the approver's queue.

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

from .. import exercise_lifecycle, range_lifecycle
from ..models import Range, RangeState
from . import invites, lifecycle, service
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
    ranges_created: int = 0
    provisioning: int = 0
    activated: int = 0
    completed: int = 0
    torn_down: int = 0
    deferred_by_window: int = 0
    built_after_window: int = 0
    approvals_lapsed: int = 0
    reminders: list[Reminder] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "ranges_created": self.ranges_created,
            "provisioning": self.provisioning,
            "activated": self.activated,
            "completed": self.completed,
            "torn_down": self.torn_down,
            "deferred_by_window": self.deferred_by_window,
            "built_after_window": self.built_after_window,
            "approvals_lapsed": self.approvals_lapsed,
            "reminders": len(self.reminders),
        }


def tick(db: Session, now: datetime | None = None) -> TickResult:
    """One pass. Commits after each booking, so one failure does not undo the rest."""
    now = now or datetime.now(UTC)
    res = TickResult()

    # 0. A booking with a template but no range: create its range now, so step 1 builds
    #    it in this same tick (decided 2026-10-06). The range is claimed onto the
    #    booking with a guarded update; a replica that loses discards its range.
    for evt in _due(
        db,
        ScheduledEvent.state == S.scheduled,
        ScheduledEvent.range_id.is_(None),
        ScheduledEvent.template_id.isnot(None),
        ScheduledEvent.start_time <= now + PROVISION_LEAD,
        ScheduledEvent.end_time > now,
    ):
        rng = range_lifecycle.create_for_booking(
            db, tenant_id=evt.tenant_id, template_id=evt.template_id, name=f"{evt.name} ({evt.start_time:%Y-%m-%d})"
        )
        won = (
            db.query(ScheduledEvent)
            .filter(ScheduledEvent.id == evt.id, ScheduledEvent.range_id.is_(None), ScheduledEvent.state == S.scheduled)
            .update({ScheduledEvent.range_id: rng.id}, synchronize_session=False)
        )
        if won != 1:
            db.rollback()
            continue
        service.audit_system(db, evt, "range_created", f"range {rng.id} from the booking's template")
        db.commit()
        res.ranges_created += 1

    # 1. Provisioning lead reached, session not over: build the range.
    held: set = set()  # bookings a maintenance window keeps from being built this tick
    for evt in _due(
        db,
        ScheduledEvent.state == S.scheduled,
        ScheduledEvent.range_id.isnot(None),
        ScheduledEvent.start_time <= now + PROVISION_LEAD,
        ScheduledEvent.end_time > now,
    ):
        if _held_by_window(db, evt, now):
            held.add(evt.id)
            continue
        if _claim(db, evt, S.provisioning):
            db.refresh(evt)  # the range the booking points at now, after any edit that won the row

            def owned(evt=evt):
                evt.auto_provisioned = True

            _, what = range_lifecycle.provision_for_booking(db, evt.range_id, before_build=owned)
            if evt.scenario_id and not evt.exercise_id:
                ex = exercise_lifecycle.create_for_booking(
                    db, tenant_id=evt.tenant_id, range_id=evt.range_id, scenario_id=evt.scenario_id, name=evt.name
                )
                evt.exercise_id, evt.auto_exercise = ex.id, True
                what += f"; exercise {ex.id} created (pending)"
            service.audit_system(db, evt, "transition", f"scheduled -> provisioning; {what}")
            db.commit()
            res.provisioning += 1

    # 1b. A booking that is under way with its range never built (it went active while a
    #     window held the build, before this was fixed, or a build refused by the worker
    #     put the range back to ``created``): build it now, unless a window still holds it.
    for evt in _due(
        db,
        ScheduledEvent.state == S.active,
        ScheduledEvent.range_id.isnot(None),
        ScheduledEvent.end_time > now,
    ):
        # tenant-safe: a booking's range, checked against its tenant when booked (resolve_range).
        rng = db.get(Range, evt.range_id)
        if rng is None or rng.state != RangeState.created:
            continue
        if _held_by_window(db, evt, now):
            held.add(evt.id)
            continue

        def owned(evt=evt):
            evt.auto_provisioned = True

        built, what = range_lifecycle.provision_for_booking(db, evt.range_id, before_build=owned)
        if built:
            service.audit_system(db, evt, "build_after_window", what)
            db.commit()
            res.built_after_window += 1
        else:
            db.rollback()

    # 2. Session started (a booking whose whole window was missed passes through here
    #    to step 3 in the same tick, without building anything). A booking a window holds
    #    stays scheduled while its session is on, so step 1 builds it once the window ends.
    for evt in _due(db, ScheduledEvent.state.in_([S.scheduled, S.provisioning]), ScheduledEvent.start_time <= now):
        if evt.id in held and evt.state == S.scheduled and _aware(evt.end_time) > now:
            continue
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

    res.deferred_by_window = len(held)

    # 3c. A class session nobody approved before it ended: it lapses (cancelled, audited).
    for evt in _due(db, ScheduledEvent.state == S.pending_approval, ScheduledEvent.end_time <= now):
        if _claim(db, evt, S.cancelled):
            service.audit_system(db, evt, "approval_lapsed", "pending_approval -> cancelled: never approved")
            db.commit()
            res.approvals_lapsed += 1

    # 4. Reminders, once per booking, to its Instructor and its class (course or roll call).
    if timedelta(0) < REMINDER_LEAD:
        for evt in _due(
            db,
            ScheduledEvent.state.in_([S.scheduled, S.provisioning]),
            ScheduledEvent.reminded_at.is_(None),
            ScheduledEvent.instructor_id.isnot(None) | ScheduledEvent.course_id.isnot(None),
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
            people = invites.recipients(db, evt)
            res.reminders += [_reminder(evt, to, attending) for to, attending in people]
            if people:
                students = sum(1 for _, attending in people if attending)
                service.audit_system(
                    db, evt, "reminder", f"emailed {len(people) - students} instructor, {students} student(s)"
                )
            db.commit()
    return res


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _held_by_window(db: Session, evt: ScheduledEvent, now: datetime) -> bool:
    """A maintenance window keeps the clock from building this booking's range now. A
    range that is already up needs no build, so nothing holds it."""
    from ..maintenance_windows import service as windows

    # tenant-safe: a booking's range, checked against its tenant when booked (resolve_range).
    rng = db.get(Range, evt.range_id) if evt.range_id else None
    if rng is not None and rng.state in range_lifecycle.UP_STATES:
        return False
    w = windows.provision_blocked(db, tenant_id=evt.tenant_id, range_id=evt.range_id, at=now)
    if w is not None:
        logger.info("booking %s: not building its range during maintenance window %s (%s)", evt.id, w.id, w.reason)
    return w is not None


def _due(db: Session, *crit) -> list[ScheduledEvent]:
    return db.query(ScheduledEvent).filter(*crit).order_by(ScheduledEvent.start_time).limit(500).all()


def _claim(db: Session, evt: ScheduledEvent, to: EventState) -> bool:
    """Win the move or step aside: another replica, or a person, got there first."""
    try:
        return lifecycle.transition(db, evt, to)
    except HTTPException:
        return False


def _reminder(evt: ScheduledEvent, to: str, attending: bool = False) -> Reminder:
    when = window_label(evt.start_time, evt.end_time)
    body = (
        f"You are attending '{evt.name}' {when}.\n"
        if attending
        else (
            f"You are teaching '{evt.name}' {when}.\n\n"
            f"The range is built {int(PROVISION_LEAD.total_seconds() // 60)} minutes before the start "
            f"and torn down {int(TEARDOWN_GRACE.total_seconds() // 60)} minutes after the end.\n"
        )
    )
    return Reminder(event_id=str(evt.id), to=to, subject=f"Reminder: {evt.name}, {when}", body=body)


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
