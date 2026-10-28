"""GA gaps B: the class flow against ADR 0014 (issue #319).

The rules under test:

- **Never forfeit a seat unless the Student was actually notified** (owner direction).
  Notified means: the onboarding or day-before email was handed to the SMTP relay
  (``class_roll_call.notified_at``), or the Student was sent an in-app notification and has
  signed in since it was issued. With email off, or a send that failed, nobody forfeits, and
  the lead sees "not notified, seat kept" on the roll call.
- The day-before reminder goes out on the day before the class in the site's time zone
  (``SITE_TIMEZONE``), not the UTC date.
- The roll-call status ``ready`` is gone (ADR 0014 never defines it).
- The lead sees why an approver rejected a roll-call addition.
- The instructor is told, in the app and by email when email is configured, when their class
  session is approved, rejected or given another time.
- A Student admitted from a roll call is flagged on ``/onboarding/state`` (the first-run
  wizard skips its access-request step for them).
- Only an admin approves Request access (ADR 0014 §5).
- ``/schedule/events`` says per booking whether the caller may move it, accept or decline a
  proposal: the server's own rule (the class's lead or an admin).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from _shared import act_as, real_tenant, real_user
from app.ad_status_sync.models import UserLastSeen
from app.auth import CurrentUser, TokenPayload, get_token_identity
from app.classes import clock as reminder_clock
from app.classes import forfeit
from app.classes import service as class_service
from app.classes.models import RollCallEntry, RollCallSettings, RollCallStatus
from app.classes.schemas import RollCallStatusOut
from app.main import app as fastapi_app
from app.models import Course, RegistrationRequest, RegistrationStatus, User, UserRole
from app.notifications.models import UserNotification

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
NINE = DAY + timedelta(hours=9)
DUE = NINE + forfeit.FORFEIT_AFTER
SECOND = timedelta(seconds=1)


class Outbox:
    """The SMTP channel: ``ok`` says whether the relay took the message."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        method, _ = (metadata or {}).get("calendar", (None, ""))
        self.sent.append({"to": to, "subject": subject, "body": body, "method": method})
        return self.ok

    def plain(self) -> list[dict]:
        return [m for m in self.sent if m["method"] is None]


@pytest.fixture
def smtp_off(monkeypatch):
    """No relay configured: the real SMTP channel logs and reports not sent."""
    for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def smtp_on(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "relay.corp.test")
    monkeypatch.setenv("SMTP_FROM", "truenorth@corp.test")
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


def _student(db, tenant_id, email: str, source: str = "ad") -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0],
             role=UserRole.student, tenant_id=tenant_id, source=source)
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db_session, client, monkeypatch):
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(100 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    db = db_session
    a = real_tenant(db, "gaps-a")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    db.add(course)
    db.flush()
    db.add(RollCallSettings(tenant_id=a.id, email_domains=["corp.test"]))
    w = {
        "a": a, "course": course,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "range_ops": real_user(db, UserRole.range_ops, a.id),
        # A roster row (CSV import) that has never signed in: linked to its entry, but invited.
        "rosie": _student(db, a.id, "rosie@corp.test", source="csv_import"),
    }
    act_as(w["lead"])
    r = client.post("/classes", json={
        "name": "C201-26-03", "course_id": str(course.id),
        "start_date": str(DAY.date()), "end_date": str((DAY + timedelta(days=4)).date()),
        "assistant_ids": [w["assistant"].id], "observer_ids": [],
    })
    assert r.status_code == 201, r.text
    w["class"] = r.json()
    return w


def _add(client, w, emails: list[str]) -> dict:
    act_as(w["lead"])
    r = client.post(f"/classes/{w['class']['id']}/roll-call", json={"emails": emails})
    assert r.status_code == 200, r.text
    return r.json()


def _book(client, w, start=NINE, hours=3, approve=True) -> dict:
    act_as(w["lead"])
    r = client.post("/schedule/events", json={
        "name": f"Lab {start:%a %H%M}", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(), "class_id": w["class"]["id"], "vcpu_total": 10,
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    if approve:
        act_as(w["admin"])
        r = client.post(f"/schedule/events/{ev['id']}/approve")
        assert r.status_code == 200, r.text
    return ev


def _entry(db, email) -> RollCallEntry:
    row = db.query(RollCallEntry).filter(RollCallEntry.email == email).one()
    db.refresh(row)
    return row


def _roll_call(client, w, at: datetime | None = None) -> dict[str, dict]:
    """The roll call as the lead reads it, at ``at`` (default: now)."""
    act_as(w["lead"])
    with patch.object(class_service, "_now", lambda: at or datetime.now(UTC)):
        r = client.get(f"/classes/{w['class']['id']}/roll-call")
    assert r.status_code == 200, r.text
    return {e["email"]: e for e in r.json()}


# ── Never forfeit a seat unless the Student was notified ─────────────────────


def test_email_off_the_seat_is_kept_and_the_lead_is_told(client, db_session, world, smtp_off):
    _add(client, world, ["newbie@corp.test"])
    newbie = _entry(db_session, "newbie@corp.test")
    assert newbie.onboarding_sent_at is not None  # claimed for the daily budget...
    assert newbie.notified_at is None  # ...but no relay took it
    _book(client, world)
    assert forfeit.tick(db_session, now=DUE + timedelta(hours=3)).forfeited == []
    assert _entry(db_session, "newbie@corp.test").status == "invited"
    row = _roll_call(client, world, at=DUE + SECOND)["newbie@corp.test"]
    assert row["notified"] is False and row["seat_kept_not_notified"] is True


def test_before_the_deadline_nothing_is_flagged(client, db_session, world, smtp_off):
    _add(client, world, ["newbie@corp.test"])
    _book(client, world)
    row = _roll_call(client, world, at=DUE - SECOND)["newbie@corp.test"]
    assert row["seat_kept_not_notified"] is False  # not past the deadline yet


def test_email_on_a_delivered_onboarding_email_counts(client, db_session, world, smtp_on):
    _add(client, world, ["newbie@corp.test"])
    assert any(m["to"] == "newbie@corp.test" for m in smtp_on.plain())
    assert _entry(db_session, "newbie@corp.test").notified_at is not None
    _book(client, world)
    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert [f.emails for f in res.forfeited] == [["newbie@corp.test"]]
    row = _roll_call(client, world)["newbie@corp.test"]
    assert row["status"] == "forfeited" and row["notified"] is True and row["seat_kept_not_notified"] is False


def test_email_on_but_the_relay_refused_it_keeps_the_seat(client, db_session, world, smtp_on):
    smtp_on.ok = False
    _add(client, world, ["newbie@corp.test"])
    assert _entry(db_session, "newbie@corp.test").notified_at is None
    _book(client, world)
    assert forfeit.tick(db_session, now=DUE + SECOND).forfeited == []
    assert _roll_call(client, world, at=DUE + SECOND)["newbie@corp.test"]["seat_kept_not_notified"] is True


def test_a_delivered_day_before_reminder_counts(client, db_session, world, smtp_on, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "UTC")
    smtp_on.ok = False
    _add(client, world, ["newbie@corp.test"])
    _book(client, world)
    smtp_on.ok = True
    eve = DAY - timedelta(hours=12)
    mails = reminder_clock.tick(db_session, now=eve)
    assert [m.to for m in mails] == ["newbie@corp.test"]
    asyncio.run(class_service.deliver(mails, db_session.get_bind()))
    assert _entry(db_session, "newbie@corp.test").notified_at is not None
    assert len(forfeit.tick(db_session, now=DUE + SECOND).forfeited) == 1


def test_in_app_counts_only_once_the_student_signed_in_after_it(client, db_session, world, smtp_off):
    _add(client, world, ["rosie@corp.test"])
    rosie = _entry(db_session, "rosie@corp.test")
    assert rosie.user_id == world["rosie"].id and rosie.status == "invited"
    assert rosie.in_app_notified_at is not None and rosie.notified_at is None
    notes = db_session.query(UserNotification).filter(UserNotification.user_id == world["rosie"].id).all()
    assert len(notes) == 1 and "C201-26-03" in notes[0].title
    _book(client, world)

    # Seen, but before the notification was issued: not told.
    db_session.add(UserLastSeen(user_id=world["rosie"].id, last_seen_at=rosie.in_app_notified_at - timedelta(hours=1)))
    db_session.flush()
    assert forfeit.tick(db_session, now=DUE + SECOND).forfeited == []
    assert _roll_call(client, world, at=DUE + SECOND)["rosie@corp.test"]["seat_kept_not_notified"] is True

    # Signed in since: the notification was there to read.
    seen = db_session.get(UserLastSeen, world["rosie"].id)
    seen.last_seen_at = rosie.in_app_notified_at + timedelta(minutes=5)
    db_session.flush()
    assert [f.emails for f in forfeit.tick(db_session, now=DUE + timedelta(minutes=2)).forfeited] == [
        ["rosie@corp.test"]]


# ── The day-before reminder in the site's time zone ──────────────────────────


def _local(day_offset: int, hour: int, tz: str) -> datetime:
    local = datetime.combine(DAY.date() + timedelta(days=day_offset), datetime.min.time(), tzinfo=ZoneInfo(tz))
    return (local + timedelta(hours=hour)).astimezone(UTC)


def test_the_reminder_goes_the_evening_before_in_the_site_time_zone(client, db_session, world, smtp_off, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
    _add(client, world, ["newbie@corp.test"])
    # 21:00 in Toronto the day before: already the class's day in UTC.
    now = _local(-1, 21, "America/Toronto")
    assert now.date() == DAY.date()
    assert [m.to for m in reminder_clock.tick(db_session, now=now)] == ["newbie@corp.test"]


def test_no_reminder_two_days_early_in_the_site_time_zone(client, db_session, world, smtp_off, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
    _add(client, world, ["newbie@corp.test"])
    # 22:00 in Toronto two days before: the day before in UTC, but not locally.
    now = _local(-2, 22, "America/Toronto")
    assert now.date() == DAY.date() - timedelta(days=1)
    assert reminder_clock.tick(db_session, now=now) == []
    assert _entry(db_session, "newbie@corp.test").reminded_at is None


def test_an_unknown_site_time_zone_falls_back_to_utc(client, db_session, world, smtp_off, monkeypatch):
    monkeypatch.setenv("SITE_TIMEZONE", "Mars/Olympus")
    _add(client, world, ["newbie@corp.test"])
    assert [m.to for m in reminder_clock.tick(db_session, now=DAY - timedelta(hours=1))] == ["newbie@corp.test"]


# ── No roll-call status "ready" ──────────────────────────────────────────────


def test_there_is_no_ready_roll_call_status():
    assert "ready" not in {s.value for s in RollCallStatus}
    assert "ready" not in {s.value for s in RollCallStatusOut}


# ── The rejection reason, for the lead ──────────────────────────────────────


def test_the_lead_sees_why_an_addition_was_rejected(client, db_session, world):
    cid = uuid.UUID(world["class"]["id"])
    entry = RollCallEntry(class_id=cid, tenant_id=world["a"].id, email="late@corp.test",
                          status=RollCallStatus.pending.value, pending_reason="no room: 10 vCPU short",
                          added_by=uuid.UUID(world["lead"].id))
    db_session.add(entry)
    db_session.flush()
    act_as(world["admin"])
    r = client.post(f"/classes/{cid}/roll-call/{entry.id}/reject", json={"reason": "class is full, next intake"})
    assert r.status_code == 200, r.text
    row = _roll_call(client, world)["late@corp.test"]
    assert row["status"] == "removed"
    assert row["rejection_reason"] == "class is full, next intake"
    # An entry the lead removed themselves has no rejection reason.
    _add(client, world, ["gone@corp.test"])
    gone = _entry(db_session, "gone@corp.test")
    act_as(world["lead"])
    assert client.delete(f"/classes/{cid}/roll-call/{gone.id}").status_code == 204
    assert _roll_call(client, world)["gone@corp.test"]["rejection_reason"] is None


# ── The instructor hears about the approver's decision ───────────────────────


def _notes(db, user: CurrentUser) -> list[UserNotification]:
    return (db.query(UserNotification).filter(UserNotification.user_id == uuid.UUID(user.id))
            .order_by(UserNotification.created_at).all())


def test_approve_reject_and_propose_notify_the_instructor_in_app_and_by_email(client, db_session, world, smtp_on):
    smtp_on.sent.clear()
    approved = _book(client, world)
    notes = _notes(db_session, world["lead"])
    assert len(notes) == 1 and "approved" in notes[0].title.lower()
    assert any(m["to"] == world["lead"].email and "approved" in m["subject"].lower() for m in smtp_on.plain())

    rejected = _book(client, world, start=NINE + timedelta(days=1), approve=False)
    act_as(world["range_ops"])
    assert client.post(f"/schedule/events/{rejected['id']}/reject", json={"reason": "range is down"}).status_code == 200
    note = _notes(db_session, world["lead"])[-1]
    assert "rejected" in note.title.lower() and "range is down" in note.message

    proposed = _book(client, world, start=NINE + timedelta(days=2), approve=False)
    act_as(world["admin"])
    later = NINE + timedelta(days=3)
    r = client.post(f"/schedule/events/{proposed['id']}/propose",
                    json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    assert r.status_code == 200, r.text
    note = _notes(db_session, world["lead"])[-1]
    assert "proposed" in note.title.lower()
    assert len(_notes(db_session, world["lead"])) == 3
    subjects = [m["subject"].lower() for m in smtp_on.plain() if m["to"] == world["lead"].email]
    assert any("rejected" in s for s in subjects) and any("proposed" in s for s in subjects)
    # The approver is never told of their own decision.
    assert _notes(db_session, world["admin"]) == [] and _notes(db_session, world["range_ops"]) == []
    assert approved["id"]


def test_with_email_off_the_instructor_is_told_in_the_app_only(client, db_session, world, monkeypatch, smtp_off):
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        _book(client, world)
    assert len(_notes(db_session, world["lead"])) == 1
    assert not [m for m in box.plain() if m["to"] == world["lead"].email]


# ── The first-run wizard knows a roll-call Student ───────────────────────────


def _identity(email, sub) -> TokenPayload:
    return TokenPayload(**{
        "sub": sub, "email": email, "email_verified": True, "given_name": "N", "family_name": "B",
        "preferred_username": sub, "groups": [], "ad_object_guid": str(uuid.uuid5(uuid.NAMESPACE_OID, sub)),
        "ad_distinguished_name": f"CN={sub},OU=Users,DC=corp,DC=test",
    })


def _as(u: User) -> CurrentUser:
    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


def test_onboarding_state_says_when_the_roll_call_admitted_them(client, db_session, world, smtp_off):
    _add(client, world, ["newbie@corp.test"])
    fastapi_app.dependency_overrides[get_token_identity] = lambda: _identity("newbie@corp.test", "ad-newbie")
    try:
        assert client.get("/auth/me").json()["status"] == "registered"
    finally:
        fastapi_app.dependency_overrides.pop(get_token_identity, None)
    newbie = db_session.query(User).filter(User.email == "newbie@corp.test").one()
    act_as(_as(newbie))
    r = client.get("/onboarding/state")
    assert r.status_code == 200 and r.json()["via_roll_call"] is True

    other = _student(db_session, world["a"].id, "walkin@corp.test")
    act_as(_as(other))
    assert client.get("/onboarding/state").json()["via_roll_call"] is False


# ── Only an admin approves Request access (ADR 0014 §5) ──────────────────────


def _pending_request(db, tenant_id) -> RegistrationRequest:
    sub = f"ad-{uuid.uuid4().hex[:8]}"
    row = RegistrationRequest(id=uuid.uuid4(), keycloak_id=sub, email=f"{sub}@corp.test", display_name=sub,
                              suggested_tenant_id=tenant_id, status=RegistrationStatus.pending)
    db.add(row)
    db.flush()
    return row


def test_an_instructor_cannot_approve_or_see_access_requests(client, db_session, world):
    req = _pending_request(db_session, world["a"].id)
    act_as(world["lead"])
    assert client.get("/registration/requests").status_code == 403
    assert client.post(f"/registration/requests/{req.id}/approve", json={"role": "student"}).status_code == 403
    assert client.post(f"/registration/requests/{req.id}/reject", json={"reason": "no"}).status_code == 403
    assert client.post("/registration/requests/bulk-approve",
                       json={"request_ids": [str(req.id)], "role": "student"}).status_code == 403
    db_session.refresh(req)
    assert req.status == RegistrationStatus.pending


def test_an_admin_approves_access_requests(client, db_session, world):
    req = _pending_request(db_session, world["a"].id)
    act_as(world["admin"])
    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "student"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


def test_roll_call_admission_still_works_without_registration_approve(client, db_session, world, smtp_off):
    """The roll call is the approval (ADR 0014 §1): the lead's role need not hold registration:approve."""
    _add(client, world, ["newbie@corp.test"])
    fastapi_app.dependency_overrides[get_token_identity] = lambda: _identity("newbie@corp.test", "ad-newbie2")
    try:
        assert client.get("/auth/me").json()["status"] == "registered"
    finally:
        fastapi_app.dependency_overrides.pop(get_token_identity, None)
    assert _entry(db_session, "newbie@corp.test").status == "signed_in"


# ── Schedule: per-booking permission flags, the server's rule ────────────────


def _listed(client, who: CurrentUser) -> dict[str, dict]:
    act_as(who)
    r = client.get("/schedule/events", params={"limit": 200})
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()["items"]}


def test_the_flags_follow_the_class_lead_or_admin_rule(client, db_session, world):
    approved = _book(client, world)
    proposed = _book(client, world, start=NINE + timedelta(days=1), approve=False)
    act_as(world["admin"])
    later = NINE + timedelta(days=2)
    assert client.post(f"/schedule/events/{proposed['id']}/propose",
                       json={"start_time": later.isoformat(),
                             "end_time": (later + timedelta(hours=3)).isoformat()}).status_code == 200

    lead = _listed(client, world["lead"])
    assert (lead[approved["id"]]["can_change"], lead[approved["id"]]["can_move"]) == (True, True)
    assert (lead[approved["id"]]["can_accept"], lead[approved["id"]]["can_decline"]) == (False, False)
    assert (lead[proposed["id"]]["can_accept"], lead[proposed["id"]]["can_decline"]) == (True, True)

    assistant = _listed(client, world["assistant"])
    for ev in (approved, proposed):
        flags = assistant[ev["id"]]
        assert (flags["can_change"], flags["can_move"], flags["can_accept"], flags["can_decline"]) == (
            False, False, False, False)
    # And the server agrees with the flags.
    act_as(world["assistant"])
    assert client.post(f"/schedule/events/{proposed['id']}/proposal/accept").status_code == 403

    admin = _listed(client, world["admin"])
    assert admin[proposed["id"]]["can_accept"] is True and admin[approved["id"]]["can_move"] is True


def test_a_cancelled_class_sessions_cannot_be_moved(client, db_session, world):
    ev = _book(client, world, approve=False)
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}").status_code == 204
    flags = _listed(client, world["lead"])[ev["id"]]
    assert (flags["can_change"], flags["can_move"]) == (False, False)


def test_a_plain_booking_is_its_instructors(client, db_session, world):
    act_as(world["assistant"])
    r = client.post("/schedule/events", json={
        "name": "Own lab", "start_time": (NINE + timedelta(days=10)).isoformat(),
        "end_time": (NINE + timedelta(days=10, hours=2)).isoformat(), "vcpu_total": 5,
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    assert _listed(client, world["assistant"])[ev["id"]]["can_move"] is True
    assert _listed(client, world["lead"])[ev["id"]]["can_move"] is False
    assert _listed(client, world["admin"])[ev["id"]]["can_move"] is True


# ── Review of #326: a re-added Student, winding down, notice subjects ────────


def test_a_readded_student_is_notified_afresh_and_only_forfeits_if_told_in_this_stint(
    client, db_session, world, smtp_on
):
    _add(client, world, ["newbie@corp.test"])
    first = _entry(db_session, "newbie@corp.test")
    assert first.notified_at is not None
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}/roll-call/{first.id}").status_code == 204

    smtp_on.ok = False  # the second stint's email is refused
    smtp_on.sent.clear()
    _add(client, world, ["newbie@corp.test"])
    again = _entry(db_session, "newbie@corp.test")
    assert [m["to"] for m in smtp_on.plain()] == ["newbie@corp.test"]  # emailed again, not skipped
    assert (again.notified_at, again.in_app_notified_at) == (None, None)  # the old stint's proof is gone
    _book(client, world)
    assert forfeit.tick(db_session, now=DUE + SECOND).forfeited == []

    smtp_on.ok = True  # and a delivered one in this stint counts
    mails = reminder_clock.tick(db_session, now=DAY - timedelta(hours=12))
    asyncio.run(class_service.deliver(mails, db_session.get_bind()))
    assert len(forfeit.tick(db_session, now=DUE + SECOND).forfeited) == 1


def test_can_wind_down_is_separate_from_can_change(client, db_session, world):
    from app.scheduler.models import EventState, ScheduledEvent

    ev = _book(client, world)
    row = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    row.state = EventState.active
    db_session.commit()
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}").status_code == 204  # the session is left under way
    flags = _listed(client, world["lead"])[ev["id"]]
    assert (flags["can_change"], flags["can_move"], flags["can_wind_down"]) == (False, False, True)
    assert _listed(client, world["admin"])[ev["id"]]["can_wind_down"] is True
    assert _listed(client, world["assistant"])[ev["id"]]["can_wind_down"] is False
    act_as(world["lead"])
    assert client.post(f"/schedule/events/{ev['id']}/complete").status_code == 200  # as the flag says


def test_a_closed_class_session_cannot_wind_down(client, db_session, world):
    from app.classes.models import Class

    ev = _book(client, world)
    db_session.get(Class, uuid.UUID(world["class"]["id"])).status = "closed"
    db_session.commit()
    assert _listed(client, world["lead"])[ev["id"]]["can_wind_down"] is False


def test_approval_notice_subjects_carry_no_line_breaks(client, db_session, world, smtp_on):
    from app.scheduler import approval_notices
    from app.scheduler.models import ScheduledEvent

    ev = _book(client, world, approve=False)
    row = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    row.name = "Lab\r\nBcc: evil@corp.test"
    notices = approval_notices.notify(db_session, row, world["admin"], "approved")
    assert notices
    for n in notices:
        assert "\r" not in n.subject and "\n" not in n.subject


def test_proposing_the_same_time_again_notifies_once(client, db_session, world, smtp_on):
    ev = _book(client, world, approve=False)
    act_as(world["admin"])
    later = NINE + timedelta(days=3)
    body = {"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()}
    assert client.post(f"/schedule/events/{ev['id']}/propose", json=body).status_code == 200
    assert client.post(f"/schedule/events/{ev['id']}/propose", json=body).status_code == 200
    assert len([n for n in _notes(db_session, world["lead"]) if "proposed" in n.title.lower()]) == 1
    other = later + timedelta(days=1)
    body = {"start_time": other.isoformat(), "end_time": (other + timedelta(hours=3)).isoformat()}
    assert client.post(f"/schedule/events/{ev['id']}/propose", json=body).status_code == 200
    assert len([n for n in _notes(db_session, world["lead"]) if "proposed" in n.title.lower()]) == 2
