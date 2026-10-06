"""Students and their own sessions (ADR 0004 slice 10, decided 2026-10-06).

A booking names a course; its active Students attend. They see their own sessions in
the app (/schedule/mine) and in their calendar feed, and get invites and reminders.
They still never see the calendar, capacity, or anyone else.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Course, Enrollment, EnrollmentStatus, User, UserRole
from app.scheduler import clock
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
SOON = (datetime.now(UTC) + timedelta(days=4)).replace(hour=13, minute=0, second=0, microsecond=0)


def _user(db, email: str, role: UserRole = UserRole.student, tenant: str = DEV_TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=email,
        display_name=email.split("@")[0],
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


def _course(db, tenant: str | None = DEV_TENANT, name: str = "C204 Security Monitoring") -> Course:
    c = Course(id=uuid.uuid4(), name=name, tenant_id=uuid.UUID(tenant) if tenant else None)
    db.add(c)
    db.flush()
    return c


def _enrol(db, user: User, course: Course, status=EnrollmentStatus.enrolled) -> None:
    db.add(Enrollment(id=uuid.uuid4(), user_id=user.id, course_id=course.id, tenant_id=user.tenant_id, status=status))
    db.flush()


@contextmanager
def signed_in(u: User):
    who = CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        tenant_id=str(u.tenant_id),
        keycloak_id="kc",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def outbox():
    sent = []

    class Box:
        async def send(self, to, subject, body, metadata=None):
            sent.append((to, (metadata or {}).get("calendar", (None,))[0], body))
            return True

    with patch("app.notifications.get_channel", return_value=Box()):
        yield sent


def _book(client, teacher: User, course: Course | None, name: str = "Blue team drill", start=SOON) -> dict:
    with signed_in(teacher):
        r = client.post(
            "/schedule/events",
            json={
                "name": name,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=2)).isoformat(),
                **({"course_id": str(course.id)} if course else {}),
            },
        )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def klass(db_session):
    """An instructor, a course with two active Students, a withdrawn one, and an outsider."""
    teacher = _user(db_session, "teacher@example.test", UserRole.instructor)
    course = _course(db_session)
    ann, bob = _user(db_session, "ann@example.test"), _user(db_session, "bob@example.test")
    gone, outsider = _user(db_session, "gone@example.test"), _user(db_session, "out@example.test")
    _enrol(db_session, ann, course)
    _enrol(db_session, bob, course, EnrollmentStatus.in_progress)
    _enrol(db_session, gone, course, EnrollmentStatus.withdrawn)
    return {"teacher": teacher, "course": course, "ann": ann, "bob": bob, "gone": gone, "outsider": outsider}


def test_a_student_sees_their_own_sessions_and_nothing_else(client, klass):
    mine = _book(client, klass["teacher"], klass["course"], "Their class")
    _book(client, klass["teacher"], None, "Staff only", SOON + timedelta(days=1))
    assert mine["course_id"] == str(klass["course"].id)

    with signed_in(klass["ann"]):
        sessions = client.get("/schedule/mine").json()
        assert [s["name"] for s in sessions] == ["Their class"]
        assert set(sessions[0]) == {"id", "name", "description", "state", "start_time", "end_time"}
        assert client.get("/schedule/events").status_code == 403  # still no calendar
        assert client.get("/schedule/capacity").status_code == 403
    for who in ("gone", "outsider"):
        with signed_in(klass[who]):
            assert client.get("/schedule/mine").json() == []


def test_a_students_feed_holds_their_sessions_without_other_people(client, klass):
    _book(client, klass["teacher"], klass["course"], "Their class")
    _book(client, klass["teacher"], None, "Staff only", SOON + timedelta(days=1))
    with signed_in(klass["ann"]):
        url = client.post("/schedule/feed-token").json()["url"].split("/api/v1", 1)[1]
    body = client.get(url).text
    assert "SUMMARY:Their class" in body
    assert "Staff only" not in body
    for name in ("teacher", "bob", "ATTENDEE", "ORGANIZER"):
        assert name not in body


def test_an_instructor_sees_what_they_teach_in_mine(client, klass):
    _book(client, klass["teacher"], klass["course"])
    with signed_in(klass["teacher"]):
        assert len(client.get("/schedule/mine").json()) == 1


def test_the_class_is_invited_and_each_gets_only_their_own_invite(client, klass, outbox):
    _book(client, klass["teacher"], klass["course"])
    assert sorted((to, m) for to, m, _ in outbox) == [
        ("ann@example.test", "REQUEST"),
        ("bob@example.test", "REQUEST"),
        ("teacher@example.test", "REQUEST"),
    ]
    student_body = next(b for to, _, b in outbox if to == "ann@example.test")
    assert student_body.startswith("You are attending")


def test_moving_a_booking_to_another_course_withdraws_it_from_the_first(client, db_session, klass, outbox):
    ev = _book(client, klass["teacher"], klass["course"])
    other = _course(db_session, name="C206 Incident Response")
    cara = _user(db_session, "cara@example.test")
    _enrol(db_session, cara, other)
    outbox.clear()
    with signed_in(klass["teacher"]):
        client.put(
            f"/schedule/events/{ev['id']}",
            json={
                "name": "Blue team drill",
                "start_time": SOON.isoformat(),
                "end_time": (SOON + timedelta(hours=2)).isoformat(),
                "course_id": str(other.id),
            },
        )
    got = sorted((to, m) for to, m, _ in outbox)
    assert ("cara@example.test", "REQUEST") in got
    assert ("ann@example.test", "CANCEL") in got and ("bob@example.test", "CANCEL") in got


def test_reminders_reach_the_students_too(db_session, klass):
    ev = ScheduledEvent(
        name="Reminded",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        course_id=klass["course"].id,
        start_time=SOON,
        end_time=SOON + timedelta(hours=2),
    )
    db_session.add(ev)
    db_session.flush()
    reminders = clock.tick(db_session, now=SOON - timedelta(hours=2)).reminders
    assert sorted(r.to for r in reminders) == ["ann@example.test", "bob@example.test"]
    assert all(r.body.startswith("You are attending") for r in reminders)


def test_a_booking_can_use_a_shared_course_but_not_another_tenants(client, db_session, klass):
    shared = _course(db_session, tenant=None, name="Shared catalogue course")
    theirs = _course(db_session, tenant=OTHER_TENANT, name="Their course")
    _book(client, klass["teacher"], shared)
    with signed_in(klass["teacher"]):
        r = client.post(
            "/schedule/events",
            json={
                "name": "x",
                "start_time": SOON.isoformat(),
                "end_time": (SOON + timedelta(hours=1)).isoformat(),
                "course_id": str(theirs.id),
            },
        )
    assert r.status_code == 404
