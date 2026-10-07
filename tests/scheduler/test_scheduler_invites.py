"""Emailed calendar invites (ADR 0004 slice 6): REQUEST when a booking becomes real or
moves (same UID, higher SEQUENCE), CANCEL when it is called off or changes hands."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
SOON = (datetime.now(UTC) + timedelta(days=9)).replace(hour=13, minute=0, second=0, microsecond=0)


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        method, calendar = (metadata or {}).get("calendar", (None, ""))
        self.sent.append({"to": to, "subject": subject, "method": method, "ics": calendar.replace("\r\n ", "")})
        return True

    def methods(self):
        return [(m["to"], m["method"]) for m in self.sent]


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


def _user(db, email: str, role: UserRole = UserRole.instructor) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=email,
        display_name=email,
        role=role,
        tenant_id=uuid.UUID(DEV_TENANT),
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def signed_in(u: User):
    who = CurrentUser(
        id=str(u.id), email=u.email, display_name=u.email, role=u.role, tenant_id=DEV_TENANT, keycloak_id="kc"
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _body(start=SOON, **extra) -> dict:
    return {
        "name": "Blue team drill",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=2)).isoformat(),
        **extra,
    }


def _field(ics: str, name: str) -> str:
    return next(line.split(":", 1)[1] for line in ics.split("\r\n") if line.split(";")[0].split(":")[0] == name)


def test_booking_invites_the_instructor(client, db_session, outbox):
    teacher = _user(db_session, "teacher@example.test")
    with signed_in(teacher):
        ev = client.post("/schedule/events", json=_body()).json()
    assert outbox.methods() == [("teacher@example.test", "REQUEST")]
    ics = outbox.sent[0]["ics"]
    assert "METHOD:REQUEST" in ics
    assert _field(ics, "UID").startswith(ev["id"])
    assert _field(ics, "SEQUENCE") == "0"
    assert _field(ics, "DTSTART") == SOON.strftime("%Y%m%dT%H%M%SZ")
    assert "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:teacher@example.test" in ics
    assert "ORGANIZER;CN=TrueNorth Range:mailto:" in ics
    assert outbox.sent[0]["subject"].startswith("Invitation: Blue team drill")


def test_moving_a_booking_sends_an_update_with_the_same_uid(client, db_session, outbox):
    teacher = _user(db_session, "teacher@example.test")
    with signed_in(teacher):
        ev = client.post("/schedule/events", json=_body()).json()
        client.put(f"/schedule/events/{ev['id']}", json=_body(SOON + timedelta(hours=3)))
    first, update = outbox.sent
    assert _field(first["ics"], "UID") == _field(update["ics"], "UID")
    assert int(_field(update["ics"], "SEQUENCE")) > int(_field(first["ics"], "SEQUENCE"))
    assert update["subject"].startswith("Updated:")


def test_handing_a_booking_to_another_instructor_withdraws_it_from_the_first(client, db_session, outbox):
    first = _user(db_session, "first@example.test")
    second = _user(db_session, "second@example.test")
    with signed_in(first):
        ev = client.post("/schedule/events", json=_body()).json()
        client.put(f"/schedule/events/{ev['id']}", json=_body(instructor_id=str(second.id)))
    assert outbox.methods() == [
        ("first@example.test", "REQUEST"),
        ("second@example.test", "REQUEST"),
        ("first@example.test", "CANCEL"),
    ]


def test_cancelling_sends_a_cancellation(client, db_session, outbox):
    teacher = _user(db_session, "teacher@example.test")
    with signed_in(teacher):
        ev = client.post("/schedule/events", json=_body()).json()
        client.post(f"/schedule/events/{ev['id']}/cancel")
    request, cancel = outbox.sent
    assert cancel["method"] == "CANCEL"
    assert "METHOD:CANCEL" in cancel["ics"] and "STATUS:CANCELLED" in cancel["ics"]
    assert _field(cancel["ics"], "UID") == _field(request["ics"], "UID")
    assert int(_field(cancel["ics"], "SEQUENCE")) > int(_field(request["ics"], "SEQUENCE"))


def test_drafts_are_not_sent_until_scheduled(client, db_session, outbox):
    teacher = _user(db_session, "teacher@example.test")
    with signed_in(teacher):
        draft = client.post("/schedule/events", json=_body(draft=True)).json()
        client.put(f"/schedule/events/{draft['id']}", json=_body(SOON + timedelta(hours=1), draft=True))
        assert outbox.sent == []
        client.post(f"/schedule/events/{draft['id']}/schedule")
    assert outbox.methods() == [("teacher@example.test", "REQUEST")]
    assert outbox.sent[0]["subject"].startswith("Invitation:")  # its first, not an update


def test_cancelling_a_draft_sends_nothing(client, db_session, outbox):
    teacher = _user(db_session, "teacher@example.test")
    with signed_in(teacher):
        draft = client.post("/schedule/events", json=_body(draft=True)).json()
        client.post(f"/schedule/events/{draft['id']}/cancel")
    assert outbox.sent == []


def test_no_instructor_no_invite(client, db_session, outbox):
    with signed_in(_user(db_session, "admin@example.test", UserRole.admin)):
        client.post("/schedule/events", json=_body())
    assert outbox.sent == []


@pytest.mark.asyncio
async def test_the_smtp_channel_attaches_a_calendar_part(monkeypatch):
    from app.notifications.smtp import SMTPChannel

    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    captured = {}

    async def fake_send(msg, **kw):
        captured["msg"] = msg

    monkeypatch.setattr("aiosmtplib.send", fake_send)
    ok = await SMTPChannel().send(
        "t@example.test", "Invitation", "body", {"calendar": ("REQUEST", "BEGIN:VCALENDAR\r\n")}
    )
    assert ok
    parts = {p.get_content_type(): p for p in captured["msg"].walk()}
    assert "text/calendar" in parts
    assert parts["text/calendar"].get_param("method") == "REQUEST"
