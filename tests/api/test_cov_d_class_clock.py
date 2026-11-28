# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""The day-before class reminder clock (``app.classes.clock``, ADR 0014 §2).

Complements test_classes_278.py and test_class_flow_gaps_319.py (the happy tick, the
cancelled class, the shared daily budget) with the rules those leave out: a Student who
is on the roll call only through an unclaimed roster row is still reminded, one who
already has a signed-in account is not; someone onboarded the same day is not reminded
on top of the onboarding email; "the day before" is the site's calendar day, not UTC's
(#319); the class scan pages through every class, not just the first page; and the
lifespan loop keeps ticking after a failed tick.

No network: the mail channel is the Outbox patch test_classes_278.py uses.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.classes import clock
from app.classes.models import RollCallEntry, RollCallSettings
from app.models import Course, User, UserRole

TODAY = datetime.now(UTC).date()


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return True


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


@pytest.fixture
def world(db_session):
    db = db_session
    a = real_tenant(db, "covd-clock")
    w = {
        "a": a,
        "lead": real_user(db, UserRole.instructor, a.id),
        "course": Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True),
    }
    db.add(RollCallSettings(tenant_id=a.id, email_domains=["corp.test"]))
    db.add(w["course"])
    db.flush()
    return w


def _student_row(db, tenant_id, email, *, source="ad") -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email=email,
        display_name=email.split("@")[0],
        role=UserRole.student,
        tenant_id=tenant_id,
        source=source,
    )
    db.add(u)
    db.flush()
    return u


def _klass(client, world, db, *, start: date, name: str = "C201-26-03") -> dict:
    act_as(world["lead"])
    r = client.post(
        "/classes",
        json={
            "name": name,
            "course_id": str(world["course"].id),
            "start_date": str(start),
            "end_date": str(start + timedelta(days=4)),
            "assistant_ids": [],
            "observer_ids": [],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _add(client, world, klass, *emails) -> list[dict]:
    act_as(world["lead"])
    r = client.post(f"/classes/{klass['id']}/roll-call", json={"emails": list(emails)})
    assert r.status_code == 200, r.text
    return r.json()["added"]


def _eve(start: date) -> datetime:
    """09:00 UTC on the calendar day before ``start`` (SITE_TIMEZONE is unset here)."""
    return datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=9)


# ── Who is reminded ──────────────────────────────────────────────────────


def test_an_unclaimed_roster_row_is_reminded_but_a_signed_in_account_is_not(client, world, db_session, outbox):
    start = TODAY + timedelta(days=2)
    klass = _klass(client, world, db_session, start=start)
    _student_row(db_session, world["a"].id, "signed@corp.test", source="ad")  # has signed in
    _student_row(db_session, world["a"].id, "roster@corp.test", source="csv_import")  # unclaimed row
    added = _add(client, world, klass, "joe@corp.test", "signed@corp.test", "roster@corp.test")
    assert len(added) == 3

    eve = _eve(start)
    assert [m.to for m in clock.tick(db_session, now=eve - timedelta(days=1))] == []  # not yet
    mails = clock.tick(db_session, now=eve)
    assert {m.to for m in mails} == {"joe@corp.test", "roster@corp.test"}
    assert "starts tomorrow" in mails[0].subject
    assert clock.tick(db_session, now=eve + timedelta(hours=1)) == []  # once per entry


def test_someone_onboarded_that_same_day_is_not_reminded_on_top_of_it(client, world, db_session, outbox):
    start = TODAY + timedelta(days=2)
    klass = _klass(client, world, db_session, start=start)
    eve = _eve(start)
    # Added the day before: the onboarding email went out then, so the reminder is due.
    _add(client, world, klass, "early@corp.test")
    _add(client, world, klass, "same_day@corp.test")  # onboarded on reminder day itself
    db_session.query(RollCallEntry).filter_by(email="early@corp.test").update(
        {RollCallEntry.onboarding_sent_at: eve - timedelta(days=1)}, synchronize_session=False
    )
    db_session.query(RollCallEntry).filter_by(email="same_day@corp.test").update(
        {RollCallEntry.onboarding_sent_at: eve},
        synchronize_session=False,  # today's onboarding email
    )
    db_session.flush()

    assert [m.to for m in clock.tick(db_session, now=eve)] == ["early@corp.test"]
    later = db_session.query(RollCallEntry).filter_by(email="same_day@corp.test").one()
    assert later.reminded_at is None  # skipped, not claimed — the next tick may yet remind


# ── The site's calendar day, not UTC's (#319) ────────────────────────────


def test_the_day_before_is_the_site_calendar_day(client, world, db_session, outbox, monkeypatch):
    """Late UTC evening: UTC says the class starts in two days, Auckland says tomorrow."""
    start = date.today() + timedelta(days=12)
    now = datetime.combine(start - timedelta(days=2), datetime.min.time(), tzinfo=UTC).replace(hour=20)
    klass = _klass(client, world, db_session, start=start)
    _add(client, world, klass, "joe@corp.test")

    monkeypatch.delenv("SITE_TIMEZONE", raising=False)
    assert clock.tick(db_session, now=now) == []  # UTC: start is not "tomorrow" yet

    monkeypatch.setenv("SITE_TIMEZONE", "Pacific/Auckland")  # UTC+13 in ( southern ) summer
    mails = clock.tick(db_session, now=now)
    assert [m.to for m in mails] == ["joe@corp.test"]
    assert db_session.query(RollCallEntry).filter_by(email="joe@corp.test").one().reminded_at is not None


# ── The class scan is paged ──────────────────────────────────────────────


def test_every_class_starting_that_day_is_reached_across_pages(client, world, db_session, outbox, monkeypatch):
    monkeypatch.setattr(clock, "PAGE", 1)  # force one class per page
    start = TODAY + timedelta(days=2)
    k1 = _klass(client, world, db_session, start=start, name="Alpha")
    k2 = _klass(client, world, db_session, start=start, name="Beta")
    _klass(client, world, db_session, start=start + timedelta(days=1), name="Gamma")  # different day
    _add(client, world, k1, "a@corp.test")
    _add(client, world, k2, "b@corp.test")

    mails = clock.tick(db_session, now=_eve(start))
    assert {m.to for m in mails} == {"a@corp.test", "b@corp.test"}  # page 2 was scanned too


# ── The lifespan loop ────────────────────────────────────────────────────


def test_the_lifespan_tick_runs_in_its_own_session(monkeypatch):
    made = []

    class _Db:
        def close(self):
            made.append("closed")

    def _open():
        made.append("opened")
        return _Db()

    monkeypatch.setattr("app.db.SessionLocal", _open)
    seen = []
    monkeypatch.setattr(clock, "tick", lambda db: seen.append(db) or [])
    assert clock._tick_in_own_session() == []
    assert len(seen) == 1 and seen[0] is not None
    assert made == ["opened", "closed"]  # the session is closed either way


def test_run_forever_survives_a_failing_tick_and_delivers_the_next_one(monkeypatch):
    results = iter([RuntimeError("db connection gone"), [object()]])
    delivered = []

    def flaky():
        step = next(results)
        if isinstance(step, Exception):
            raise step
        return step

    async def fake_deliver(mails, bind):
        delivered.append(len(mails))

    class _NoRealLoop:
        """The asyncio surface run_forever uses; no threads, and it ends after 4 sleeps."""

        CancelledError = asyncio.CancelledError

        def __init__(self):
            self.sleeps = 0

        async def sleep(self, _):
            self.sleeps += 1
            if self.sleeps >= 4:
                raise asyncio.CancelledError

        async def to_thread(self, fn):
            return fn()

    fake = _NoRealLoop()
    monkeypatch.setattr(clock, "TICK_SECONDS", 0.0)
    monkeypatch.setattr(clock, "_tick_in_own_session", flaky)
    monkeypatch.setattr(clock.service, "deliver", fake_deliver)
    monkeypatch.setattr(clock, "asyncio", fake)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(clock.run_forever())
    assert fake.sleeps == 4  # three ticks ran (one failed, one delivered, one exhausted) before the stop
    assert delivered == [1]  # the failed tick was skipped, the next one still delivered
