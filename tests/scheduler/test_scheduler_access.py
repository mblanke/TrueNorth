"""Who may see and change the schedule (docs/adr/0004-scheduler-module.md).

Everyone but Students reads it; only admins and instructors write it; every event list
and create is scoped to the caller's tenant. Before ADR 0004 the router was gated on
exercise:read, which Students hold, and listing returned every tenant's events.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()),
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


def _event(db, tenant: str, name: str = "evt") -> ScheduledEvent:
    start = datetime.now(UTC) + timedelta(days=30)
    ev = ScheduledEvent(
        name=name,
        state=EventState.scheduled,
        tenant_id=uuid.UUID(tenant),
        start_time=start,
        end_time=start + timedelta(hours=2),
    )
    db.add(ev)
    db.flush()
    return ev


def _body(name: str = "Blue team drill") -> dict:
    start = datetime.now(UTC) + timedelta(days=60)
    return {
        "name": name,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=3)).isoformat(),
        "vcpu_total": 4,
        "ram_mb_total": 8192,
    }


READ_PATHS = ["/schedule/events", "/schedule/capacity", "/schedule/timeline?days=1"]


@pytest.mark.parametrize("path", READ_PATHS)
def test_students_cannot_see_the_schedule(client, path):
    with acting_as(UserRole.student):
        assert client.get(path).status_code == 403


def test_students_cannot_check_capacity(client):
    with acting_as(UserRole.student):
        r = client.post("/schedule/check", json={"start_time": _body()["start_time"], "end_time": _body()["end_time"]})
    assert r.status_code == 403


@pytest.mark.parametrize("role", [UserRole.admin, UserRole.instructor, UserRole.range_ops, UserRole.observer])
@pytest.mark.parametrize("path", READ_PATHS)
def test_staff_see_the_schedule(client, role, path):
    with acting_as(role):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_only_admins_and_instructors_write(client, db_session, role):
    ev = _event(db_session, DEV_TENANT)
    with acting_as(role):
        assert client.post("/schedule/events", json=_body()).status_code == 403
        assert client.put(f"/schedule/events/{ev.id}", json=_body()).status_code == 403
        assert client.post(f"/schedule/events/{ev.id}/activate").status_code == 403
        assert client.post(f"/schedule/events/{ev.id}/complete").status_code == 403
        assert client.delete(f"/schedule/events/{ev.id}").status_code == 403
    assert db_session.get(ScheduledEvent, ev.id).state == EventState.scheduled


def test_instructor_books_into_their_own_tenant(client):
    with acting_as(UserRole.instructor, tenant=OTHER_TENANT):
        r = client.post("/schedule/events", json=_body())
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == OTHER_TENANT


def test_event_list_is_tenant_scoped(client, db_session):
    mine = _event(db_session, DEV_TENANT, "mine")
    theirs = _event(db_session, OTHER_TENANT, "theirs")
    with acting_as(UserRole.observer):
        ids = {e["id"] for e in client.get("/schedule/events").json()["items"]}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


def test_another_tenants_event_is_404(client, db_session):
    theirs = _event(db_session, OTHER_TENANT)
    with acting_as(UserRole.instructor):
        assert client.get(f"/schedule/events/{theirs.id}").status_code == 404
        assert client.delete(f"/schedule/events/{theirs.id}").status_code == 404
