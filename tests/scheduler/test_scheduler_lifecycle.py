"""Booking lifecycle and double-booking (ADR 0004 slice 3).

draft -> scheduled -> provisioning -> active -> completed, cancelled from any state
before completed. Moves are guarded updates. A range or an instructor cannot be in
two bookings at once.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import AuditLog, Range, RangeState, User, UserRole
from app.scheduler import lifecycle
from app.scheduler.models import EventState, ScheduledEvent
from fastapi import HTTPException

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
DAY = (datetime.now(UTC) + timedelta(days=50)).replace(hour=0, minute=0, second=0, microsecond=0)


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT, user_id: uuid.UUID | None = None):
    who = CurrentUser(
        id=str(user_id or uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _user(db, role: UserRole = UserRole.instructor, tenant: str = DEV_TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


def _range(db, tenant: str = DEV_TENANT) -> Range:
    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(tenant), name="r", template_id=uuid.uuid4())
    r.state = RangeState.created
    db.add(r)
    db.flush()
    return r


def _book(start: datetime, hours: float = 2, **extra) -> dict:
    return {
        "name": extra.pop("name", "Session"),
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(),
        **extra,
    }


def _create(client, body: dict, role: UserRole = UserRole.instructor, **who) -> dict:
    with acting_as(role, **who):
        r = client.post("/schedule/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _post(client, path: str, role: UserRole = UserRole.instructor):
    with acting_as(role):
        return client.post(path)


# -- Lifecycle ------------------------------------------------------------------


def test_the_happy_path_and_its_audit_trail(client, db_session):
    ev = _create(client, _book(DAY + timedelta(hours=9)))
    assert ev["state"] == "scheduled"
    assert _post(client, f"/schedule/events/{ev['id']}/activate").json()["state"] == "active"
    assert _post(client, f"/schedule/events/{ev['id']}/complete").json()["state"] == "completed"
    moves = [
        a.detail
        for a in db_session.query(AuditLog)
        .filter(AuditLog.resource_id == ev["id"], AuditLog.action == "transition")
        .order_by(AuditLog.timestamp)
        .all()
    ]
    assert sorted(moves) == sorted(["scheduled -> active", "active -> completed"])


@pytest.mark.parametrize(
    ("setup", "action", "expect"),
    [
        ([], "complete", 409),  # scheduled cannot complete without running
        (["activate", "complete"], "cancel", 409),  # completed is final
        (["activate", "complete"], "activate", 409),
        (["cancel"], "activate", 409),  # cancelled is final
        (["activate"], "activate", 200),  # retry: no-op
        (["cancel"], "cancel", 200),  # retry: no-op
    ],
)
def test_transitions_are_guarded(client, setup, action, expect):
    ev = _create(client, _book(DAY + timedelta(hours=9)))
    for step in setup:
        assert _post(client, f"/schedule/events/{ev['id']}/{step}").status_code == 200
    r = _post(client, f"/schedule/events/{ev['id']}/{action}")
    assert r.status_code == expect, r.text


def test_a_retry_is_not_recorded_twice(client, db_session):
    ev = _create(client, _book(DAY + timedelta(hours=9)))
    for _ in range(3):
        assert _post(client, f"/schedule/events/{ev['id']}/activate").status_code == 200
    moved = db_session.query(AuditLog).filter(AuditLog.resource_id == ev["id"], AuditLog.action == "transition")
    assert moved.count() == 1


def test_a_stale_read_cannot_move_a_booking(db_session):
    """Two requests read `scheduled`; the first activates. The second's guarded update
    finds nothing to move and refuses, instead of moving it twice."""
    ev = ScheduledEvent(
        name="race",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        start_time=DAY,
        end_time=DAY + timedelta(hours=1),
    )
    db_session.add(ev)
    db_session.flush()
    db_session.query(ScheduledEvent).filter(ScheduledEvent.id == ev.id).update(
        {ScheduledEvent.state: EventState.cancelled}, synchronize_session=False
    )
    # `ev` still believes it is scheduled.
    with pytest.raises(HTTPException) as exc:
        lifecycle.transition(db_session, ev, EventState.active)
    assert exc.value.status_code == 409


def test_delete_cancels_and_keeps_the_row(client, db_session):
    ev = _create(client, _book(DAY + timedelta(hours=9)))
    with acting_as(UserRole.instructor):
        assert client.delete(f"/schedule/events/{ev['id']}").status_code == 204
    row = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    db_session.refresh(row)
    assert row.state == EventState.cancelled


def test_only_draft_or_scheduled_can_be_edited(client):
    ev = _create(client, _book(DAY + timedelta(hours=9)))
    _post(client, f"/schedule/events/{ev['id']}/activate")
    with acting_as(UserRole.instructor):
        r = client.put(f"/schedule/events/{ev['id']}", json=_book(DAY + timedelta(hours=11)))
    assert r.status_code == 409
    assert "active" in r.json()["detail"]


def test_a_draft_holds_nothing_until_it_is_scheduled(client, monkeypatch):
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", "1024")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    draft = _create(client, _book(DAY + timedelta(hours=9), ram_mb_total=4096, draft=True))
    assert draft["state"] == "draft"
    r = _post(client, f"/schedule/events/{draft['id']}/schedule")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("RAM: need 4 GB, 1 GB free")


def test_unknown_event_id_is_404_not_500(client):
    assert _post(client, "/schedule/events/not-a-uuid/activate").status_code == 404


# -- Conflicts ------------------------------------------------------------------


def test_a_range_cannot_be_double_booked(client, db_session):
    rng = _range(db_session)
    start = DAY + timedelta(hours=9)
    _create(client, _book(start, range_id=str(rng.id), name="Morning"))
    with acting_as(UserRole.instructor):
        clash = client.post("/schedule/events", json=_book(start + timedelta(hours=1), range_id=str(rng.id)))
        # The range needs its teardown grace and the next provisioning lead between bookings.
        too_close = client.post(
            "/schedule/events", json=_book(start + timedelta(hours=2, minutes=30), range_id=str(rng.id))
        )
        ok = client.post("/schedule/events", json=_book(start + timedelta(hours=2, minutes=45), range_id=str(rng.id)))
        other_range = client.post("/schedule/events", json=_book(start, range_id=str(_range(db_session).id)))
    assert clash.status_code == 409
    assert clash.json()["detail"].startswith("Range is already booked for 'Morning'")
    assert too_close.status_code == 409
    assert ok.status_code == 201, ok.text
    assert other_range.status_code == 201, other_range.text


def test_a_cancelled_booking_frees_its_range(client, db_session):
    rng = _range(db_session)
    start = DAY + timedelta(hours=9)
    first = _create(client, _book(start, range_id=str(rng.id)))
    _post(client, f"/schedule/events/{first['id']}/cancel")
    _create(client, _book(start, range_id=str(rng.id)))


def test_another_tenants_range_cannot_be_booked(client, db_session):
    theirs = _range(db_session, tenant=OTHER_TENANT)
    with acting_as(UserRole.instructor):
        r = client.post("/schedule/events", json=_book(DAY, range_id=str(theirs.id)))
    assert r.status_code == 404


def test_an_instructor_cannot_teach_two_sessions_at_once(client, db_session):
    teacher = _user(db_session)
    start = DAY + timedelta(hours=9)
    first = _create(client, _book(start, name="Blue team"), user_id=teacher.id)
    assert first["instructor_id"] == str(teacher.id)
    assert first["created_by"] == str(teacher.id)
    with acting_as(UserRole.instructor, user_id=teacher.id):
        clash = client.post("/schedule/events", json=_book(start + timedelta(hours=1)))
        back_to_back = client.post("/schedule/events", json=_book(start + timedelta(hours=2)))
    assert clash.status_code == 409
    assert clash.json()["detail"].startswith("Instructor is already teaching 'Blue team'")
    assert back_to_back.status_code == 201, back_to_back.text


def test_an_admin_books_on_behalf_of_an_instructor_in_their_tenant(client, db_session):
    teacher = _user(db_session)
    outsider = _user(db_session, tenant=OTHER_TENANT)
    student = _user(db_session, role=UserRole.student)
    ev = _create(client, _book(DAY, instructor_id=str(teacher.id)), role=UserRole.admin)
    assert ev["instructor_id"] == str(teacher.id)
    with acting_as(UserRole.admin):
        assert client.post("/schedule/events", json=_book(DAY, instructor_id=str(outsider.id))).status_code == 422
        assert client.post("/schedule/events", json=_book(DAY, instructor_id=str(student.id))).status_code == 422
