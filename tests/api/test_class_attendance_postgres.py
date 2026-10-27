"""Attendance recorded by several API replicas at once, on PostgreSQL (ADR 0014 §12, issue #283).

SQLite serialises writers, so a race there proves nothing. These run only with
TEST_POSTGRES_ADMIN_URL (CI's test-python job; ``postgres_engine`` in tests/conftest.py is a
fresh copy of ``alembic upgrade head``).

Each replica inserts a session's missing rows in savepoints under the (session, entry) unique
constraint. Two replicas record the same session; the first holds its inserted rows, before
its commit, for longer than the second takes to arrive, so the second's inserts wait on the
unique index and then fail, and are skipped. Each Student ends with exactly one row, and an
instructor's correction committed meanwhile is never overwritten.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.classes import attendance
from app.classes.models import Class, ClassAttendance, ClassSession, RollCallEntry
from app.models import AuditLog, Course, Tenant, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

START = (datetime.now(UTC) - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
END = START + timedelta(hours=3)
NOW = END + timedelta(minutes=5)
HOLD = 1.5  # seconds the first replica holds its inserts before committing
NAMES = ("ann", "bob", "cat")


@pytest.fixture
def world(postgres_engine):
    eng = postgres_engine
    tenant, course, lead, cls, event, link = (uuid.uuid4() for _ in range(6))
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, email="lead@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant, keycloak_id=f"kc-{lead.hex[:8]}"))
        users = {n: uuid.uuid4() for n in NAMES}
        for n, uid in users.items():
            db.add(User(id=uid, email=f"{n}@corp.test", display_name=n, role=UserRole.student, tenant_id=tenant,
                        keycloak_id=f"kc-{uid.hex[:8]}", source="ad"))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today() + timedelta(days=3), lead_instructor_id=lead, status="open"))
        db.add(ScheduledEvent(id=event, name="Lab", state=EventState.completed, tenant_id=tenant, instructor_id=lead,
                              start_time=START, end_time=END))
        db.flush()
        db.add(ClassSession(id=link, class_id=cls, event_id=event, tenant_id=tenant, created_by=lead))
        for n, uid in users.items():
            db.add(RollCallEntry(class_id=cls, tenant_id=tenant, email=f"{n}@corp.test", user_id=uid,
                                 status="signed_in", joined_at=START - timedelta(days=2)))
        db.commit()
    return {"engine": eng, "class": cls, "session": link, "users": users}


def test_two_replicas_record_each_student_once(world, monkeypatch):
    first_in = threading.Event()
    real_record = attendance.record_session

    def slow_record(db, cls, cs, evt, entries=None):
        n = real_record(db, cls, cs, evt, entries)
        if not first_in.is_set():
            first_in.set()
            time.sleep(HOLD)  # hold the inserted rows (uncommitted) while the other replica arrives
        return n

    monkeypatch.setattr(attendance, "record_session", slow_record)
    results: list = [None, None]
    errors: list = []

    def replica(i):
        try:
            with Session(world["engine"]) as db:
                results[i] = attendance.tick(db, now=NOW)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    a = threading.Thread(target=replica, args=(0,))
    a.start()
    assert first_in.wait(10)
    b = threading.Thread(target=replica, args=(1,))
    b.start()
    a.join(20)
    b.join(20)
    assert not errors, errors
    assert not results[0].failed and not results[1].failed
    assert results[0].recorded + results[1].recorded == len(NAMES)
    with Session(world["engine"]) as db:
        rows = db.query(ClassAttendance).filter_by(session_id=world["session"]).all()
        assert len(rows) == len(NAMES)
        assert {r.status for r in rows} == {"absent"}


def test_a_correction_racing_the_clock_is_never_overwritten(world):
    with Session(world["engine"]) as db:
        cls = db.get(Class, world["class"])
        cs = db.get(ClassSession, world["session"])
        evt = db.get(ScheduledEvent, cs.event_id)
        ann = db.query(RollCallEntry).filter_by(email="ann@corp.test").one()
        lead = type("Actor", (), {"id": str(cls.lead_instructor_id)})()
        attendance.correct(db, cls, cs, evt, ann, "excused", "dentist", lead, now=NOW)
        db.commit()
        db.add(AuditLog(user_id=world["users"]["ann"], tenant_id=cls.tenant_id, action=attendance.RANGE_OPEN,
                        resource_type="range", resource_id=str(uuid.uuid4()), timestamp=START))
        db.commit()
    with Session(world["engine"]) as db:
        res = attendance.tick(db, now=NOW)
        assert res.recorded == 2
    with Session(world["engine"]) as db:
        row = db.query(ClassAttendance).join(RollCallEntry, RollCallEntry.id == ClassAttendance.entry_id).filter(
            RollCallEntry.email == "ann@corp.test").one()
        assert (row.status, row.source) == ("excused", "instructor")
