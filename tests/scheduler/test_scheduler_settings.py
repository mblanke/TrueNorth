"""``GET /schedule/settings``: the scheduler settings the UI shows in words (#318).

The Classes and Schedule pages said "built 30 minutes before" and the approval queue said
"none in the next two weeks" whatever the site set (SCHEDULER_PROVISION_LEAD_MIN,
SCHEDULER_NEXT_FREE_DAYS). They now read them from here, with the site's time zone
(SITE_TIMEZONE). Staff who read the schedule may; a Student may not.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.scheduler import router as scheduler_router
from app.scheduler import service

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=role.value,
                      role=role, tenant_id=DEV_TENANT, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", [UserRole.admin, UserRole.instructor, UserRole.range_ops])
def test_staff_read_the_settings(client, role, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
    with acting_as(role):
        r = client.get("/schedule/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lead_minutes"] == 30
    assert body["grace_minutes"] == 15
    assert body["next_free_days"] == service.NEXT_FREE_DAYS == 14
    assert body["site_timezone"] == "America/Toronto"


def test_settings_follow_the_site_configuration(client, monkeypatch):
    monkeypatch.setattr(scheduler_router, "PROVISION_LEAD", timedelta(minutes=45))
    monkeypatch.setattr(service, "NEXT_FREE_DAYS", 7)
    monkeypatch.delenv("SITE_TIMEZONE", raising=False)
    with acting_as(UserRole.admin):
        body = client.get("/schedule/settings").json()
    assert (body["lead_minutes"], body["next_free_days"], body["site_timezone"]) == (45, 7, "UTC")


def test_an_unknown_time_zone_reads_as_utc(client, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "Mars/Olympus")
    with acting_as(UserRole.admin):
        assert client.get("/schedule/settings").json()["site_timezone"] == "UTC"


def test_a_student_may_not_read_them(client):
    with acting_as(UserRole.student):
        assert client.get("/schedule/settings").status_code == 403
