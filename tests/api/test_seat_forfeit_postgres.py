"""Seat forfeit on several API replicas at once, on PostgreSQL (ADR 0014 §4, issue #280).

SQLite serialises writers, so a race there proves nothing. These run only with
TEST_POSTGRES_ADMIN_URL (CI's test-python job; ``postgres_engine`` in tests/conftest.py is a
fresh copy of ``alembic upgrade head``).

Each replica claims an entry with a guarded update (``status = invited AND forfeited_at IS
NULL``) and frees seats only for the entries it won. Two replicas tick at the same moment;
the first holds its row locks, after its claims and before its commit, for longer than the
second takes to arrive, so a missing guard would let both free the same seats.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.auth import CurrentUser
from app.classes import forfeit
from app.classes import service as class_service
from app.classes import sessions as class_sessions
from app.classes.models import Class, ClassSession, RollCallEntry, RollCallSettings
from app.models import AuditLog, Course, Tenant, User, UserRole
from app.scheduler.capacity import Resources
from app.scheduler.models import BookingApproval, EventState, ScheduledEvent
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

NINE = (datetime.now(UTC) + timedelta(days=20)).replace(hour=9, minute=0, second=0, microsecond=0)
DUE = NINE + forfeit.FORFEIT_AFTER + timedelta(seconds=1)
HOLD = 1.5  # seconds the first replica holds its claims before committing


@pytest.fixture
def world(postgres_engine, monkeypatch):
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(10, 0, 0))
    eng = postgres_engine
    tenant, course, lead, cls, event = (uuid.uuid4() for _ in range(5))
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, email="lead@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant, keycloak_id=f"kc-{lead.hex[:8]}"))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today() + timedelta(days=30), lead_instructor_id=lead, status="open"))
        db.add(ScheduledEvent(id=event, name="Lab", state=EventState.scheduled, tenant_id=tenant,
                              instructor_id=lead, start_time=NINE, end_time=NINE + timedelta(hours=3),
                              vcpu_total=60))
        db.flush()
        db.add(ClassSession(class_id=cls, event_id=event, tenant_id=tenant, created_by=lead))
        db.add(BookingApproval(event_id=event, tenant_id=tenant, requested_by=lead, decided_by=lead,
                               decided_at=datetime.now(UTC)))
        db.add(RollCallSettings(tenant_id=tenant, email_domains=["corp.test"]))
        sent = datetime.now(UTC) - timedelta(days=1)  # their onboarding emails went out
        for name in ("ann", "bob", "cat"):
            db.add(RollCallEntry(class_id=cls, tenant_id=tenant, email=f"{name}@corp.test", status="invited",
                                 added_by=lead, onboarding_sent_at=sent))
        # Lost their seat earlier; the lead reinstates them while the clock runs.
        db.add(RollCallEntry(class_id=cls, tenant_id=tenant, email="dan@corp.test", status="forfeited",
                             added_by=lead, onboarding_sent_at=sent, forfeited_at=sent))
        db.commit()
    lead_user = CurrentUser(id=str(lead), email="lead@corp.test", display_name="Lead", role=UserRole.instructor,
                            tenant_id=str(tenant), keycloak_id=f"kc-{lead.hex[:8]}")
    return {"engine": eng, "class": cls, "event": event, "lead": lead_user}


def test_two_replicas_forfeit_each_seat_once(world, monkeypatch):
    real_free = class_sessions.free_seats
    first_claimed = threading.Event()

    def slow_free(db, cls, seats):
        out = real_free(db, cls, seats)
        if not first_claimed.is_set():
            first_claimed.set()
            time.sleep(HOLD)  # hold the claimed rows' locks
        return out

    monkeypatch.setattr(class_sessions, "free_seats", slow_free)
    results: list = [None, None]
    errors: list = []

    def replica(i):
        try:
            with Session(world["engine"]) as db:
                results[i] = forfeit.tick(db, now=DUE)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    a = threading.Thread(target=replica, args=(0,))
    a.start()
    assert first_claimed.wait(10)
    b = threading.Thread(target=replica, args=(1,))
    b.start()
    a.join(20)
    b.join(20)
    assert not errors, errors

    won = [e for r in results for f in r.forfeited for e in f.emails]
    assert sorted(won) == ["ann@corp.test", "bob@corp.test", "cat@corp.test"]
    assert sum(len(r.mails) for r in results) == 1  # one notice, from the replica that won
    assert sorted(i.to for r in results for i in r.invites) == sorted(won)  # one CANCEL each
    with Session(world["engine"]) as db:
        evt = db.get(ScheduledEvent, world["event"])
        assert evt.vcpu_total == 30  # three seats of 10, freed once
        assert evt.sequence == 1
        assert {e.status for e in db.query(RollCallEntry)} == {"forfeited"}  # dan's untouched too
        assert db.query(AuditLog).filter(AuditLog.action == "roll_call_forfeit").count() == 1


def test_a_sign_in_and_the_clock_never_both_win(world):
    """Admission (or ``note_sign_in``) and the forfeit claim the same row with guarded updates
    on ``invited``: whichever commits first wins, and the other matches nothing."""
    with Session(world["engine"]) as clock, Session(world["engine"]) as signin:
        entry = clock.query(RollCallEntry).filter_by(email="ann@corp.test").one()
        signed = signin.query(RollCallEntry).filter(RollCallEntry.email == "ann@corp.test",
                                                    RollCallEntry.status == "invited").update(
            {RollCallEntry.status: "signed_in"}, synchronize_session=False)
        assert signed == 1
        done = threading.Event()
        claimed: list = []

        def claim():
            claimed.append(forfeit._claim(clock, entry, DUE))  # blocks on the row lock
            done.set()

        t = threading.Thread(target=claim)
        t.start()
        assert not done.wait(0.5)  # waiting for the sign-in's transaction
        signin.commit()
        t.join(10)
        assert claimed == [False]
        clock.rollback()
    with Session(world["engine"]) as db:
        assert db.query(RollCallEntry).filter_by(email="ann@corp.test").one().status == "signed_in"


def test_the_clock_and_a_reinstatement_both_land_on_the_session(world, monkeypatch):
    """#280 third review (item 7): the clock frees three seats while the lead reinstates a
    fourth Student on the same session. Each change is an increment in SQL, so the second
    waits for the first's row lock and adds to what it committed: both land, exactly."""
    real_free = class_sessions.free_seats
    clock_holds = threading.Event()

    def slow_free(db, cls, seats):
        out = real_free(db, cls, seats)  # the session row is now locked by the clock
        clock_holds.set()
        time.sleep(HOLD)
        return out

    monkeypatch.setattr(class_sessions, "free_seats", slow_free)
    errors: list = []

    def clock():
        try:
            with Session(world["engine"]) as db:
                forfeit.tick(db, now=DUE)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def reinstate():
        try:
            with Session(world["engine"]) as db:
                cls = db.get(Class, world["class"])
                db.get(ScheduledEvent, world["event"])  # read before the clock commits: a stale copy
                dan = db.query(RollCallEntry).filter_by(email="dan@corp.test").one()
                forfeit.reinstate(db, cls, dan, world["lead"])
                db.commit()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    a = threading.Thread(target=clock)
    a.start()
    assert clock_holds.wait(10)
    b = threading.Thread(target=reinstate)
    b.start()
    a.join(20)
    b.join(20)
    assert not errors, errors
    with Session(world["engine"]) as db:
        evt = db.get(ScheduledEvent, world["event"])
        assert evt.vcpu_total == 60 - 30 + 10
        assert evt.sequence == 2  # one raise each: the CANCELs and the REQUEST differ
        assert db.query(RollCallEntry).filter_by(email="dan@corp.test").one().status == "invited"


def test_a_kelvin_sign_lookalike_never_claims_an_entry(world):
    """lower() in PostgreSQL folds U+212A (Kelvin sign) onto "k": "\u212aate@corp.test" must
    not sign in, link or keep the seat of kate@corp.test's roll-call entry."""
    with Session(world["engine"]) as db:
        cls = db.get(Class, world["class"])
        db.add(RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email="kate@corp.test", status="invited",
                             onboarding_sent_at=datetime.now(UTC) - timedelta(days=1)))
        fake = User(id=uuid.uuid4(), email="\u212aate@corp.test", display_name="K", role=UserRole.student,
                    tenant_id=cls.tenant_id, keycloak_id="kc-kelvin", source="ad")
        db.add(fake)
        db.commit()
        assert [u.id for u in class_service.accounts_with_email(db, "kate@corp.test")] == [fake.id]  # it folds
        assert forfeit.note_sign_in(db, fake) == 0
        forfeit.tick(db, now=DUE)
        kate = db.query(RollCallEntry).filter_by(email="kate@corp.test").one()
        db.refresh(kate)
        assert kate.status == "forfeited" and kate.user_id is None

