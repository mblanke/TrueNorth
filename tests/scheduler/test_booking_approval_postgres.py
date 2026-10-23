"""Approvals competing for the same capacity, on PostgreSQL (ADR 0014 §15-16, issue #281).

SQLite serialises writers, so a race there proves nothing. These run only with
TEST_POSTGRES_ADMIN_URL (CI's test-python job; ``postgres_engine`` in tests/conftest.py is a
fresh copy of ``alembic upgrade head``).

Approve checks a session's fit and moves it to ``scheduled`` under the scheduler's booking
lock, ``pg_advisory_xact_lock``: transaction-scoped, so it works behind pgbouncer in
transaction pooling mode and is released by the commit or rollback. Two approvers acting
at once on two sessions that only fit one at a time: exactly one wins, the other gets 409
with the next free slot. Each race holds the first side, after its check and before its
commit, for longer than it takes the second to arrive, so a missing lock would let both
through.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser
from app.models import AuditLog, Tenant, User, UserRole
from app.scheduler import approval, lifecycle, service
from app.scheduler.models import BookingApproval, EventState, ScheduledEvent
from fastapi import HTTPException
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

NINE = (datetime.now(UTC) + timedelta(days=20)).replace(hour=9, minute=0, second=0, microsecond=0)
HOLD = 1.5  # seconds the first approver holds the lock after its check


@pytest.fixture
def world(postgres_engine, monkeypatch):
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(100 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    eng = postgres_engine
    tenant = uuid.uuid4()
    people = {name: uuid.uuid4() for name in ("admin", "range_ops", "lead_a", "lead_b")}
    roles = {"admin": UserRole.admin, "range_ops": UserRole.range_ops, "lead_a": UserRole.instructor,
             "lead_b": UserRole.instructor}
    events = {}
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        for name, uid in people.items():
            db.add(User(id=uid, email=f"{name}@corp.test", display_name=name, role=roles[name], tenant_id=tenant,
                        keycloak_id=f"kc-{uid.hex[:8]}"))
        db.flush()
        # Two class sessions at the same time, each 60 of the 100 vCPU: one fits, not both.
        for name, lead in (("A", "lead_a"), ("B", "lead_b")):
            ev = ScheduledEvent(name=f"Session {name}", state=EventState.pending_approval, tenant_id=tenant,
                                instructor_id=people[lead], start_time=NINE, end_time=NINE + timedelta(hours=3),
                                vcpu_total=60)
            db.add(ev)
            db.flush()
            db.add(BookingApproval(event_id=ev.id, tenant_id=tenant, requested_by=people[lead],
                                   requested_at=datetime.now(UTC)))
            events[name] = ev.id
        db.commit()

    def user(name: str) -> CurrentUser:
        return CurrentUser(id=str(people[name]), email=f"{name}@corp.test", display_name=name, role=roles[name],
                           tenant_id=str(tenant), keycloak_id=f"kc-{people[name].hex[:8]}")

    return {"engine": eng, "events": events, "user": user}


def _race(first, second, first_checked: threading.Event) -> list:
    """Start ``first``; once it has checked (and, with the lock, holds it), start ``second``."""
    results: list = [None, None]

    def run(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            results[i] = exc

    t1 = threading.Thread(target=run, args=(0, first))
    t1.start()
    assert first_checked.wait(20), "the first approver never got as far as its check"
    t2 = threading.Thread(target=run, args=(1, second))
    t2.start()
    for t in (t1, t2):
        t.join(30)
    assert not t1.is_alive() and not t2.is_alive(), "a side never finished"
    return results


def _approve(eng, who: CurrentUser, event_id: uuid.UUID):
    with Session(eng) as db:
        service.serialize_bookings(db)
        evt = db.get(ScheduledEvent, event_id)
        moved, _ = approval.approve(db, who, evt)
        db.commit()
        return moved


@pytest.fixture
def slow_first_move(monkeypatch):
    """The first approval to pass its check pauses (holding whatever it holds) before it
    moves the booking; the event says it got there."""
    checked = threading.Event()
    real = lifecycle.transition

    def transition(db, evt, to):
        if to == EventState.scheduled and not checked.is_set():
            checked.set()
            time.sleep(HOLD)
        return real(db, evt, to)

    monkeypatch.setattr(lifecycle, "transition", transition)
    return checked


def test_two_approvals_for_the_last_room_one_wins(world, slow_first_move):
    eng, ev, user = world["engine"], world["events"], world["user"]
    results = _race(
        lambda: _approve(eng, user("admin"), ev["A"]),
        lambda: _approve(eng, user("range_ops"), ev["B"]),
        slow_first_move,
    )
    assert results[0] is True, results[0]
    loser = results[1]
    assert isinstance(loser, HTTPException) and loser.status_code == 409, loser
    assert any(r.startswith("vCPU: need 60, 40 free") for r in loser.detail["reasons"])
    assert loser.detail["next_free"] is not None
    with Session(eng) as db:
        states = {k: db.get(ScheduledEvent, v).state for k, v in ev.items()}
        assert states == {"A": EventState.scheduled, "B": EventState.pending_approval}
        assert db.query(AuditLog).filter(AuditLog.action == "approve").count() == 1


def test_two_approvers_on_the_same_session_move_it_once(world, slow_first_move):
    eng, ev, user = world["engine"], world["events"], world["user"]
    results = _race(
        lambda: _approve(eng, user("admin"), ev["A"]),
        lambda: _approve(eng, user("range_ops"), ev["A"]),
        slow_first_move,
    )
    assert results == [True, False]  # the second finds it approved: a no-op
    with Session(eng) as db:
        assert db.get(ScheduledEvent, ev["A"]).state == EventState.scheduled
        assert db.query(AuditLog).filter(AuditLog.action == "approve").count() == 1


def test_an_approval_and_an_accepted_proposal_for_the_last_room(world, slow_first_move):
    """The instructor accepting a proposed time competes under the same lock."""
    eng, ev, user = world["engine"], world["events"], world["user"]
    with Session(eng) as db:  # B was offered A's time; its instructor accepts as A is approved
        a = db.get(BookingApproval, ev["B"])
        a.proposed_start, a.proposed_end = NINE, NINE + timedelta(hours=3)
        a.proposed_by = uuid.UUID(user("admin").id)
        b = db.get(ScheduledEvent, ev["B"])
        b.start_time, b.end_time = NINE + timedelta(days=1), NINE + timedelta(days=1, hours=3)
        db.commit()

    def accept():
        with Session(eng) as db:
            service.serialize_bookings(db)
            approval.accept(db, user("lead_b"), db.get(ScheduledEvent, ev["B"]))
            db.commit()
            return True

    results = _race(lambda: _approve(eng, user("admin"), ev["A"]), accept, slow_first_move)
    assert results[0] is True
    assert isinstance(results[1], HTTPException) and results[1].status_code == 409
    with Session(eng) as db:
        assert db.get(ScheduledEvent, ev["B"]).state == EventState.pending_approval
