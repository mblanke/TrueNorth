# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""The class attendance clock's edge paths (``app.classes.attendance``, ADR 0014 §12; #283).

Complements test_class_attendance_closeout_283.py, which covers the attendance rule, the
idempotent tick and the instructor grid. This module takes the paths that file leaves:

- a correction that creates the row the clock never recorded (``correct`` on a session with
  no auto row yet), and a correction refused for a Student off the roll call (removed or
  still pending — a forfeited one is still correctable);
- the two ways ``note_range_open`` records nothing: an exercise range no held class session
  is booked on, and a class range with no sessions left to break the dedupe window;
- the replica race the clock is built for: a duplicate roll-call entry in one recording
  loses the unique constraint and loses gracefully;
- a tick whose commit fails: that session is rolled back and reported, the others still
  record, and the next tick picks the failed one up;
- ``counts`` on nothing and on a status it does not know;
- the lifespan plumbing: the clock ticks in its own session, and ``run_forever`` keeps
  ticking after a tick raises and logs the ones that record.

No network; the mail channel is not touched (the clock sends nothing).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import real_tenant, real_user
from app.classes import attendance
from app.classes.models import (
    Class,
    ClassAttendance,
    ClassRange,
    ClassSession,
    RollCallEntry,
)
from app.models import (
    AuditLog,
    Course,
    Range,
    RangeState,
    Template,
    User,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent
from fastapi import HTTPException

NOW = datetime.now(UTC).replace(microsecond=0)
D1 = (NOW - timedelta(days=2)).replace(hour=9, minute=0, second=0)
HOURS = timedelta(hours=3)
JOINED = NOW - timedelta(days=5)


@pytest.fixture
def w(db_session):
    """A tenant with an open class, two Students on the roll call, each with a seat range,
    and one ended session (day 1) with no evidence and no attendance rows yet."""
    db = db_session
    a = real_tenant(db, "covd-att")
    w = {
        "a": a,
        "lead": real_user(db, UserRole.instructor, a.id),
        "course": Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True),
        "tpl": Template(id=uuid.uuid4(), name="Seat", yaml="nodes: [{id: ws, os: kali}]\n", tenant_id=a.id),
    }
    db.add_all([w["course"], w["tpl"]])
    db.flush()
    cls = Class(
        id=uuid.uuid4(),
        tenant_id=a.id,
        name="C201-26-03",
        course_id=w["course"].id,
        start_date=(NOW - timedelta(days=3)).date(),
        end_date=(NOW + timedelta(days=3)).date(),
        lead_instructor_id=uuid.UUID(w["lead"].id),
        status="open",
    )
    db.add(cls)
    db.flush()
    w["cls"] = cls
    w["students"], w["entries"], w["seats"] = {}, {}, {}
    for name in ("ann", "bob"):
        u = User(
            id=uuid.uuid4(),
            keycloak_id=f"kc-{uuid.uuid4()}",
            email=f"{name}@corp.test",
            display_name=name.title(),
            role=UserRole.student,
            tenant_id=a.id,
            source="ad",
        )
        db.add(u)
        db.flush()
        e = RollCallEntry(
            id=uuid.uuid4(),
            class_id=cls.id,
            tenant_id=a.id,
            email=u.email,
            user_id=u.id,
            status="signed_in",
            joined_at=JOINED,
        )
        db.add(e)
        db.flush()
        rng = Range(
            id=uuid.uuid4(), name=f"seat-{name}", template_id=w["tpl"].id, tenant_id=a.id, state=RangeState.ready
        )
        db.add(rng)
        db.flush()
        db.add(
            ClassRange(
                tenant_id=a.id,
                class_id=cls.id,
                template_id=w["tpl"].id,
                kind="seat",
                entry_id=e.id,
                seat_key=str(e.id),
                range_id=rng.id,
                builds=1,
                built_at=JOINED,
            )
        )
        w["students"][name], w["entries"][name], w["seats"][name] = u, e, rng
    w["s1"] = _session(db, w, "Day 1", D1)
    db.flush()
    return w


def _session(db, w, name: str, start: datetime, state=EventState.completed) -> tuple:
    evt = ScheduledEvent(
        id=uuid.uuid4(),
        name=name,
        state=state,
        tenant_id=w["a"].id,
        instructor_id=uuid.UUID(w["lead"].id),
        start_time=start,
        end_time=start + HOURS,
        template_id=w["tpl"].id,
    )
    db.add(evt)
    db.flush()
    cs = ClassSession(
        id=uuid.uuid4(), class_id=w["cls"].id, event_id=evt.id, tenant_id=w["a"].id, range_mode="individual"
    )
    db.add(cs)
    db.flush()
    return cs, evt


# ── Corrections ──────────────────────────────────────────────────────────────


def test_a_correction_creates_the_row_the_clock_never_recorded(db_session, w):
    """A session the clock has not reached (close-out, or a tick that is still due): the
    instructor's correction is the row's first version, and the audit says so."""
    db = db_session
    cs, evt = w["s1"]
    corrected_at = NOW - timedelta(minutes=5)
    row = attendance.correct(
        db, w["cls"], cs, evt, w["entries"]["ann"], "excused", "court date", w["lead"], now=corrected_at
    )
    db.flush()
    assert (row.status, row.source, row.note) == ("excused", "instructor", "court date")
    assert row.corrected_by == uuid.UUID(w["lead"].id) and row.corrected_at == corrected_at
    assert row.evidence_at is None  # no evidence: an instructor row is not an auto row
    audit = db.query(AuditLog).filter_by(action="class_attendance_correct").one()
    assert "not recorded -> excused" in audit.detail and "court date" in audit.detail
    assert audit.user_id == uuid.UUID(w["lead"].id)


def test_a_correction_refuses_a_student_off_the_roll_call(db_session, w):
    """Removed and still-pending entries owe no attendance; a forfeited one does (absent is
    the clock's finding, but an instructor may still correct it)."""
    db = db_session
    cs, evt = w["s1"]
    for status in ("removed", "pending"):
        w["entries"]["bob"].status = status
        db.flush()
        with pytest.raises(HTTPException) as refused:
            attendance.correct(db, w["cls"], cs, evt, w["entries"]["bob"], "excused", None, w["lead"])
        assert refused.value.status_code == 409
        assert "not on the roll call" in refused.value.detail
    w["entries"]["bob"].status = "forfeited"  # still owes the sessions in between
    db.flush()
    row = attendance.correct(db, w["cls"], cs, evt, w["entries"]["bob"], "excused", None, w["lead"])
    db.flush()
    assert row.status == "excused"
    assert db.query(ClassAttendance).count() == 1  # only the forfeited correction was written
    assert db.query(AuditLog).filter_by(action="class_attendance_correct").count() == 1


# ── Counting ─────────────────────────────────────────────────────────────────


def test_counts_start_at_zero_and_count_what_is_there():
    assert attendance.counts([]) == {"present": 0, "absent": 0, "excused": 0}
    rows = [ClassAttendance(status=s) for s in ("present", "present", "absent", "excused")]
    assert attendance.counts(rows) == {"present": 2, "absent": 1, "excused": 1}
    rows.append(ClassAttendance(status="late"))  # not a status any more: still counted
    assert attendance.counts(rows)["late"] == 1 and attendance.counts(rows)["present"] == 2


# ── The signal: when an open records nothing ────────────────────────────────


def test_an_open_of_a_class_range_with_no_sessions_breaks_no_dedupe_window(db_session, w):
    """The dedupe window is only broken by a session starting since: a range no session of
    any class is booked on (nor owned through one) never gets past it."""
    db = db_session
    bare = Range(id=uuid.uuid4(), name="no class", template_id=w["tpl"].id, tenant_id=w["a"].id, state=RangeState.ready)
    db.add(bare)
    now = attendance.db_now(db)
    db.add(
        AuditLog(
            user_id=w["students"]["ann"].id,
            tenant_id=w["a"].id,
            action=attendance.RANGE_OPEN,
            resource_type="range",
            resource_id=str(bare.id),
            timestamp=now - timedelta(minutes=1),
        )
    )
    db.flush()
    seen = {"class_seat_id": "x"}
    assert attendance.note_range_open(db, w["students"]["ann"].id, bare, seen, now=now) is False
    assert db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN, resource_id=str(bare.id)).count() == 1


def test_an_exercise_range_no_class_session_is_booked_on_records_nothing(db_session, w):
    db = db_session
    rng = Range(
        id=uuid.uuid4(), name="exercise range", template_id=w["tpl"].id, tenant_id=w["a"].id, state=RangeState.ready
    )
    db.add(rng)
    db.flush()
    before = db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN).count()
    # ``participation`` reports only the exercise: attendance needs a held session of an open
    # class booked on this range, with the Student on its roll call. There is none.
    assert (
        attendance.note_range_open(
            db, w["students"]["ann"].id, rng, {"exercise_id": str(uuid.uuid4())}, now=attendance.db_now(db)
        )
        is False
    )
    assert db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN).count() == before


# ── The replica race ─────────────────────────────────────────────────────────


def test_a_duplicated_roll_call_entry_loses_the_unique_race_gracefully(db_session, w):
    """Each (session, entry) row is inserted once: a second insert of the same entry — what a
    replica that lost the race sees — raises inside its savepoint and moves on."""
    db = db_session
    cs, evt = w["s1"]
    added = attendance.record_session(db, w["cls"], cs, evt, [w["entries"]["ann"], w["entries"]["ann"]])
    assert added == 1
    assert db.query(ClassAttendance).filter_by(session_id=cs.id).count() == 1
    row = db.query(ClassAttendance).filter_by(session_id=cs.id).one()
    assert (row.entry_id, row.status, row.source) == (w["entries"]["ann"].id, "absent", "auto")


# ── A failing tick ───────────────────────────────────────────────────────────


def test_a_session_whose_commit_fails_is_reported_not_raised(db_session, w):
    """A commit that fails is caught: the tick reports the session by id and returns instead
    of raising out of the clock. Its rows were rolled back with it, so the session stays due
    and the next tick records it again."""
    db = db_session

    def blipping_commit():
        raise RuntimeError("the database blipped")

    db.commit = blipping_commit  # an instance attribute shadows the method, as conftest's dispatch does
    try:
        res = attendance.tick(db, now=NOW)
    finally:
        del db.commit  # the instance attribute; the class method is back
    assert res.failed == [w["s1"][0].id]
    assert (res.sessions, res.recorded) == (1, 2)  # both Students' rows were built, then undone


# ── The clock's plumbing ─────────────────────────────────────────────────────


def test_the_lifespan_tick_runs_in_its_own_session(monkeypatch):
    closed = []

    class _QuietDb:
        """A database with no due sessions; the tick over it records nothing."""

        def query(self, *a, **k):
            return self

        def join(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def all(self):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr("app.db.SessionLocal", _QuietDb)
    res = attendance._tick_in_own_session()
    assert (res.sessions, res.recorded, res.failed) == (0, 0, [])
    assert closed == [True]  # the session is closed even though the tick raised nothing


def test_run_forever_keeps_ticking_after_a_failed_one_and_logs_the_recorded_ones(monkeypatch, caplog):
    class _NoRealLoop:
        """The asyncio surface run_forever uses: no threads, no sleeps, ends after 4 ticks."""

        CancelledError = asyncio.CancelledError

        def __init__(self):
            self.sleeps = 0
            self.delivered = []

        async def sleep(self, _):
            self.sleeps += 1
            if self.sleeps >= 4:
                raise asyncio.CancelledError

        async def to_thread(self, fn):
            if self.sleeps == 1:
                raise RuntimeError("the database is out of reach")
            self.delivered.append(self.sleeps)
            return attendance.TickResult(recorded=self.sleeps, sessions=1)

    fake = _NoRealLoop()
    monkeypatch.setattr(attendance, "asyncio", fake)
    with (
        caplog.at_level(logging.INFO, logger="truenorth.api.classes.attendance"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(attendance.run_forever())
    assert fake.sleeps == 4  # three ticks ran (one failed, two delivered) before the stop
    assert fake.delivered == [2, 3]  # the failed tick was skipped, the next ones still recorded
    assert "2 row(s) over 1 session(s)" in caplog.text  # each delivered tick is logged
    assert "3 row(s) over 1 session(s)" in caplog.text
    assert "tick failed" in caplog.text  # and so is the one that raised
