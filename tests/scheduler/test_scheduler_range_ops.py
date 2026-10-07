"""The scheduler builds and tears down ranges through range_ops (CR1-06), like a person.

- A build is a recorded operation: the outbox row, sent after its commit; with the
  broker down it stays pending (re-sent later) and the booking still owns the build.
- The same refusals apply: an operation in flight, a worker still holding the lease.
  A refused build records no ownership, so the clock will not tear down a range it
  did not build.
- A lab session's range is driven only by its session: it cannot be booked, and the
  clock will not build or destroy it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import AuditLog, Range, RangeState, UserRole
from app.range_ops.models import RangeOperation
from app.scheduler import clock
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
START = (datetime.now(UTC) + timedelta(days=20)).replace(hour=13, minute=0, second=0, microsecond=0)
END = START + timedelta(hours=3)
LEAD, GRACE = timedelta(minutes=30), timedelta(minutes=15)


@pytest.fixture
def dispatched():
    sent: list[tuple[str, tuple]] = []

    def fake(name, *args):
        sent.append((name, args))
        return f"task-{len(sent)}"

    with patch("app.celery_client.dispatch", side_effect=fake):
        yield sent


def _range(db, state: RangeState = RangeState.created) -> Range:
    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(DEV_TENANT), name="r", template_id=uuid.uuid4())
    r.state = state
    db.add(r)
    db.flush()
    return r


def _booking(db, rng: Range) -> ScheduledEvent:
    ev = ScheduledEvent(
        name="Blue team drill",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        range_id=rng.id,
        start_time=START,
        end_time=END,
    )
    db.add(ev)
    db.flush()
    return ev


def _ops(db, rng: Range) -> list[RangeOperation]:
    return db.query(RangeOperation).filter(RangeOperation.range_id == rng.id).order_by(RangeOperation.generation).all()


def _clock_audit(db, ev: ScheduledEvent) -> str:
    rows = db.query(AuditLog.detail).filter(AuditLog.resource_id == str(ev.id)).all()
    return " | ".join(d for (d,) in rows)


def test_a_clock_build_is_a_recorded_operation(db_session, dispatched):
    rng = _range(db_session)
    ev = _booking(db_session, rng)
    clock.tick(db_session, now=START - LEAD)

    (op,) = _ops(db_session, rng)
    assert (op.action, op.status, op.task_id) == ("provision", "dispatched", "task-1")
    assert op.requested_by is None  # the scheduler, not a person
    db_session.refresh(ev)
    assert ev.state == EventState.provisioning and ev.auto_provisioned is True
    assert dispatched == [("provision_range", (str(rng.id),))]


def test_with_the_broker_down_the_build_stays_pending_and_owned(db_session):
    rng = _range(db_session)
    ev = _booking(db_session, rng)
    with patch("app.celery_client.dispatch", return_value=None):
        clock.tick(db_session, now=START - LEAD)

    (op,) = _ops(db_session, rng)
    assert op.status == "pending" and op.error["code"] == "broker_unavailable"
    db_session.refresh(rng)
    db_session.refresh(ev)
    assert rng.state == RangeState.provisioning
    assert ev.auto_provisioned is True  # it will be re-sent; the booking owns it
    assert "will be re-sent" in _clock_audit(db_session, ev)


def test_a_build_refused_by_an_operation_in_flight_records_no_ownership(db_session, dispatched):
    rng = _range(db_session)
    db_session.add(
        RangeOperation(
            id=uuid.uuid4(),
            tenant_id=rng.tenant_id,
            range_id=rng.id,
            action="stop",
            generation=1,
            request_hash="x",
            status="pending",
            dispatch_attempts=0,
        )
    )
    ev = _booking(db_session, rng)
    db_session.flush()
    clock.tick(db_session, now=START - LEAD)

    db_session.refresh(ev)
    db_session.refresh(rng)
    assert ev.state == EventState.provisioning  # the booking moves on; the range is not ours
    assert ev.auto_provisioned is False
    assert rng.state == RangeState.created
    assert dispatched == []
    assert "still in progress" in _clock_audit(db_session, ev)

    # ...so the end of the session tears nothing down.
    clock.tick(db_session, now=START)
    clock.tick(db_session, now=END + GRACE)
    assert dispatched == []


def test_a_build_refused_while_a_worker_holds_the_lease(db_session, dispatched):
    from app.range_leases import RangeLease

    rng = _range(db_session)
    db_session.add(RangeLease(range_id=rng.id, holder="w1", expires_at=datetime.now(UTC) + timedelta(minutes=5)))
    ev = _booking(db_session, rng)
    db_session.flush()
    clock.tick(db_session, now=START - LEAD)

    db_session.refresh(ev)
    assert ev.auto_provisioned is False
    assert _ops(db_session, rng) == [] and dispatched == []
    assert "worker is still acting" in _clock_audit(db_session, ev)


def test_teardown_is_a_destroy_operation(db_session, dispatched):
    rng = _range(db_session)
    ev = _booking(db_session, rng)
    clock.tick(db_session, now=START - LEAD)
    rng.state = RangeState.ready  # the worker reported the build
    db_session.flush()
    clock.tick(db_session, now=START)
    clock.tick(db_session, now=END + GRACE)

    assert [(o.action, o.status) for o in _ops(db_session, rng)] == [
        ("provision", "succeeded"),
        ("destroy", "dispatched"),
    ]
    db_session.refresh(ev)
    assert ev.auto_provisioned is False
    assert [n for n, _ in dispatched] == ["provision_range", "destroy_range"]


def test_the_clock_leaves_a_lab_sessions_range_alone(db_session, dispatched):
    rng = _range(db_session)
    ev = _booking(db_session, rng)  # booked before it became a lab's (or written directly)
    with patch("app.lab_sessions.service.lab_range_ids", return_value={rng.id}):
        clock.tick(db_session, now=START - LEAD)

    db_session.refresh(ev)
    db_session.refresh(rng)
    assert ev.auto_provisioned is False and rng.state == RangeState.created
    assert _ops(db_session, rng) == [] and dispatched == []
    assert "lab session" in _clock_audit(db_session, ev)


def test_a_lab_sessions_range_cannot_be_booked(db_session, client):
    rng = _range(db_session)
    db_session.commit()
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email="a@x.test",
        display_name="a",
        role=UserRole.admin,
        tenant_id=DEV_TENANT,
        keycloak_id="kc",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        with patch("app.lab_sessions.service.lab_range_ids", return_value={rng.id}):
            r = client.post(
                "/schedule/events",
                json={
                    "name": "x",
                    "start_time": START.isoformat(),
                    "end_time": END.isoformat(),
                    "range_id": str(rng.id),
                },
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 409, r.text
    assert "lab session" in r.json()["detail"]
    assert db_session.query(ScheduledEvent).filter(ScheduledEvent.range_id == rng.id).count() == 0
