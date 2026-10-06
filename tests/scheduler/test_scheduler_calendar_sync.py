"""External calendar sync (ADR 0004 slice 7): booking changes reach CALENDAR_BACKEND,
and a backend failure never fails the booking."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.scheduler.calendar_backends import BaseCalendarBackend

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
SOON = (datetime.now(UTC) + timedelta(days=11)).replace(hour=9, minute=0, second=0, microsecond=0)


class Recorder(BaseCalendarBackend):
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, int]] = []
        self.fail = fail

    async def publish(self, event):
        self.calls.append(("publish", event.uid, event.sequence))
        if self.fail:
            raise RuntimeError("calendar down")
        return "ext-1"

    async def cancel(self, event):
        self.calls.append(("cancel", event.uid, event.sequence))

    async def health_check(self):
        return True


@contextmanager
def as_instructor():
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email="i@example.test",
        display_name="i",
        role=UserRole.instructor,
        tenant_id=DEV_TENANT,
        keycloak_id="kc",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _body() -> dict:
    return {"name": "Drill", "start_time": SOON.isoformat(), "end_time": (SOON + timedelta(hours=1)).isoformat()}


def test_booking_and_cancelling_reach_the_backend(client):
    rec = Recorder()
    with patch("app.scheduler.calendar_backends.get_calendar_backend", return_value=rec), as_instructor():
        ev = client.post("/schedule/events", json=_body()).json()
        client.post(f"/schedule/events/{ev['id']}/cancel")
    uid = f"{ev['id']}@scheduler.truenorth-range"
    assert rec.calls == [("publish", uid, 0), ("cancel", uid, 1)]


def test_a_backend_failure_does_not_fail_the_booking(client):
    with (
        patch("app.scheduler.calendar_backends.get_calendar_backend", return_value=Recorder(fail=True)),
        as_instructor(),
    ):
        r = client.post("/schedule/events", json=_body())
    assert r.status_code == 201
