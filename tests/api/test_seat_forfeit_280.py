"""Classes 3: seat forfeit 2 hours after the start, and reinstatement (ADR 0014 §4; #280).

The rules under test (app/classes/forfeit.py):

- A roll-call Student still ``invited`` 2 hours after the class's *first approved* session
  started loses their seat: ``forfeited`` with ``forfeited_at``. No approved session, no
  forfeit. Sessions awaiting approval never count.
- "Signed in" is the entry reaching ``signed_in``: by admission, by being added holding an
  account that has signed in, or (an entry linked to an existing account) when that account
  next authenticates (``GET /auth/me``).
- The seat is freed through ``sessions.free_seats`` (SEQUENCE raised) and the Student gets a
  CANCEL; the lead and assistants get one email, outside the daily email budget; audited.
- Guarded updates: any number of replicas tick, each seat is forfeited once.
- Only this class's roll-call entry changes: never the account, the enrolment, other classes.
- Admission never goes through a forfeited entry; /auth/me names the class to ask about.
- The lead or an admin reinstates (one click): signed_in if they have signed in since,
  otherwise invited; the seat is taken again if it fits, otherwise it waits for an approver.
  A reinstated entry is never forfeited again.

Time travel: ``forfeit.tick(db, now=...)``, as the scheduler's and the reminder's clocks are tested.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.auth import TokenPayload, get_token_identity
from app.classes import forfeit
from app.classes import service as class_service
from app.classes import sessions as class_sessions
from app.classes.models import Class, RollCallEntry, RollCallSettings
from app.main import app as fastapi_app
from app.models import AuditLog, Course, Enrollment, User, UserRole
from app.scheduler.capacity import Resources
from app.scheduler.models import EventState, ScheduledEvent
from fastapi import HTTPException
from sqlalchemy.orm.attributes import set_committed_value

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
NINE = DAY + timedelta(hours=9)
DUE = NINE + forfeit.FORFEIT_AFTER
SECOND = timedelta(seconds=1)


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        method, ics = (metadata or {}).get("calendar", (None, ""))
        self.sent.append({"to": to, "subject": subject, "body": body, "method": method, "ics": ics})
        return True

    def to(self, method=None) -> set[str]:
        return {m["to"] for m in self.sent if m["method"] == method}

    def clear(self):
        self.sent.clear()


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


@pytest.fixture
def seats(monkeypatch):
    """Ten vCPU a seat (#282's per-Student ranges, through the seam #281 left)."""
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(10, 0, 0))


def _student(db, tenant_id, email: str, source: str = "ad") -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0],
             role=UserRole.student, tenant_id=tenant_id, source=source)
    db.add(u)
    db.flush()
    return u


def _class(client, w, who, name="C201-26-03", course=None, assistants=()) -> dict:
    act_as(who)
    r = client.post("/classes", json={
        "name": name, "course_id": str((course or w["course"]).id),
        "start_date": str(DAY.date()), "end_date": str((DAY + timedelta(days=4)).date()),
        "assistant_ids": [str(a) for a in assistants], "observer_ids": [w["observer"].id],
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def world(db_session, client, monkeypatch, outbox):
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(100 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    db = db_session
    a, b = real_tenant(db, "forfeit-a"), real_tenant(db, "forfeit-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    b_course = Course(id=uuid.uuid4(), name="B course", tenant_id=b.id, is_published=True)
    db.add_all([course, b_course])
    db.flush()
    for t in (a, b):
        db.add(RollCallSettings(tenant_id=t.id, email_domains=["corp.test"]))
    w = {
        "a": a, "b": b, "course": course, "b_course": b_course,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "other_instructor": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "range_ops": real_user(db, UserRole.range_ops, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        # Has signed in before: on the roll call as signed_in from the start.
        "ann": _student(db, a.id, "ann@corp.test"),
        # A roster row (CSV import) that has never signed in: linked, but invited.
        "rosie": _student(db, a.id, "rosie@corp.test", source="csv_import"),
    }
    w["class"] = _class(client, w, w["lead"], assistants=[w["assistant"].id])
    act_as(w["lead"])
    r = client.post(f"/classes/{w['class']['id']}/roll-call",
                    json={"emails": ["ann@corp.test", "newbie@corp.test", "rosie@corp.test"]})
    assert r.status_code == 200 and len(r.json()["added"]) == 3, r.text
    outbox.clear()
    return w


def _cls(db, w) -> Class:
    return db.get(Class, uuid.UUID(w["class"]["id"]))


def _entry(db, email, w=None) -> RollCallEntry:
    q = db.query(RollCallEntry).filter(RollCallEntry.email == email)
    if w is not None:
        q = q.filter(RollCallEntry.class_id == uuid.UUID(w["class"]["id"]))
    row = q.one()
    db.refresh(row)
    return row


def _book(client, w, start=NINE, hours=3, vcpu=40, class_id=None, approve=True) -> dict:
    act_as(w["lead"])
    r = client.post("/schedule/events", json={
        "name": f"Lab {start:%a %H%M}", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(),
        "class_id": class_id or w["class"]["id"], "vcpu_total": vcpu,
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    if approve:
        act_as(w["admin"])
        r = client.post(f"/schedule/events/{ev['id']}/approve")
        assert r.status_code == 200, r.text
    return ev


def _event(db, ev) -> ScheduledEvent:
    row = db.get(ScheduledEvent, uuid.UUID(ev["id"]))
    db.refresh(row)
    return row


def _statuses(db, w) -> dict[str, str]:
    cid = uuid.UUID(w["class"]["id"])
    rows = db.query(RollCallEntry).filter(RollCallEntry.class_id == cid).all()
    for r in rows:
        db.refresh(r)
    return {r.email: r.status for r in rows}


def _identity(email, sub, **over) -> TokenPayload:
    data = {
        "sub": sub, "email": email, "email_verified": True, "given_name": "N", "family_name": "B",
        "preferred_username": sub, "groups": [], "ad_object_guid": str(uuid.uuid5(uuid.NAMESPACE_OID, sub)),
        "ad_distinguished_name": f"CN={sub},OU=Users,DC=corp,DC=test",
    }
    data.update(over)
    return TokenPayload(**data)


@pytest.fixture
def sign_in(client):
    def _go(identity: TokenPayload) -> dict:
        fastapi_app.dependency_overrides[get_token_identity] = lambda: identity
        r = client.get("/auth/me")
        assert r.status_code == 200, r.text
        return r.json()

    yield _go
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


def _audits(db, w, action) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.resource_id == w["class"]["id"], AuditLog.action == action).all()


# ── The 2-hour boundary ────────────────────────────────────────────────────


def test_the_2_hour_boundary(client, db_session, world):
    _book(client, world)
    assert forfeit.tick(db_session, now=DUE - SECOND).forfeited == []
    assert _statuses(db_session, world) == {
        "ann@corp.test": "signed_in", "newbie@corp.test": "invited", "rosie@corp.test": "invited"}

    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert [sorted(f.emails) for f in res.forfeited] == [["newbie@corp.test", "rosie@corp.test"]]
    assert _statuses(db_session, world) == {
        "ann@corp.test": "signed_in", "newbie@corp.test": "forfeited", "rosie@corp.test": "forfeited"}
    newbie = _entry(db_session, "newbie@corp.test")
    assert forfeit._aware(newbie.forfeited_at) == DUE + SECOND


def test_exactly_at_the_deadline_the_seat_goes(db_session, client, world):
    _book(client, world)
    assert len(forfeit.tick(db_session, now=DUE).forfeited) == 1


def test_no_approved_session_no_forfeit(client, db_session, world):
    late = NINE + timedelta(days=30)
    assert forfeit.tick(db_session, now=late).forfeited == []  # no session at all
    _book(client, world, approve=False)  # one awaiting approval
    assert forfeit.tick(db_session, now=late).forfeited == []
    assert set(_statuses(db_session, world).values()) == {"signed_in", "invited"}


def test_only_the_first_approved_session_counts(client, db_session, world):
    _book(client, world, start=NINE, approve=False)  # earliest, but never approved
    second = NINE + timedelta(days=1)
    _book(client, world, start=second)
    _book(client, world, start=NINE + timedelta(days=2))
    assert forfeit.tick(db_session, now=NINE + timedelta(hours=5)).forfeited == []
    assert forfeit.tick(db_session, now=second + forfeit.FORFEIT_AFTER - SECOND).forfeited == []
    assert len(forfeit.tick(db_session, now=second + forfeit.FORFEIT_AFTER + SECOND).forfeited) == 1


def test_a_finished_first_session_still_counts(client, db_session, world):
    ev = _book(client, world, hours=1)
    row = _event(db_session, ev)
    row.state = EventState.completed
    db_session.flush()
    assert len(forfeit.tick(db_session, now=DUE + SECOND).forfeited) == 1


def test_a_late_addition_gets_its_own_two_hours(client, db_session, world):
    _book(client, world)
    act_as(world["lead"])
    assert client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]}).status_code == 200
    late = _entry(db_session, "late@corp.test")
    assert late.joined_at is not None
    late.joined_at = NINE + timedelta(hours=3)  # as if added three hours into the class
    db_session.flush()
    forfeit.tick(db_session, now=DUE + timedelta(hours=1))
    assert _entry(db_session, "late@corp.test").status == "invited"
    assert _entry(db_session, "newbie@corp.test").status == "forfeited"
    forfeit.tick(db_session, now=NINE + timedelta(hours=5) + SECOND)
    assert _entry(db_session, "late@corp.test").status == "forfeited"


# ── Signed in ──────────────────────────────────────────────────────────────


def test_admission_before_the_deadline_keeps_the_seat(client, db_session, world, sign_in):
    _book(client, world)
    assert sign_in(_identity("newbie@corp.test", "ad-newbie"))["status"] == "registered"
    forfeit.tick(db_session, now=DUE + SECOND)
    assert _entry(db_session, "newbie@corp.test").status == "signed_in"


def test_an_existing_linked_student_counts_as_signed_in_when_they_authenticate(client, db_session, world, sign_in):
    """Rosie's roster row was adopted outside the roll call (Request access approved by an
    admin): her entry is still invited and linked, and admission will never run for her
    again. Her next authentication signs the entry in."""
    rosie = world["rosie"]
    rosie.source = "ad"
    rosie.keycloak_id = "ad-rosie"
    db_session.flush()
    _book(client, world)
    assert _entry(db_session, "rosie@corp.test").status == "invited"
    me = sign_in(_identity("rosie@corp.test", "ad-rosie"))
    assert me["status"] == "registered" and me["roll_call_forfeited"] == []
    entry = _entry(db_session, "rosie@corp.test")
    assert entry.status == "signed_in" and entry.signed_in_at is not None
    assert len(_audits(db_session, world, "roll_call_signed_in")) == 1
    forfeit.tick(db_session, now=DUE + SECOND)
    assert _entry(db_session, "rosie@corp.test").status == "signed_in"
    # Signing in again changes nothing.
    sign_in(_identity("rosie@corp.test", "ad-rosie"))
    assert len(_audits(db_session, world, "roll_call_signed_in")) == 1


def test_authenticating_after_the_forfeit_does_not_undo_it(client, db_session, world, sign_in):
    rosie = world["rosie"]
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    rosie.source, rosie.keycloak_id = "ad", "ad-rosie"  # adopted through Request access, too late
    db_session.flush()
    me = sign_in(_identity("rosie@corp.test", "ad-rosie"))
    assert me["status"] == "registered" and me["roll_call_forfeited"] == ["C201-26-03"]
    assert _entry(db_session, "rosie@corp.test").status == "forfeited"
    # Off this roll call: the class is no longer theirs to see.
    act_as(class_service_user(rosie))
    assert client.get("/classes").json() == []


def class_service_user(u: User):
    from app.auth import CurrentUser

    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


# ── Replicas ───────────────────────────────────────────────────────────────


def test_ticking_again_or_on_another_replica_does_nothing_more(client, db_session, world, seats):
    ev = _book(client, world)
    first = forfeit.tick(db_session, now=DUE + SECOND)
    seq, vcpu = _event(db_session, ev).sequence, _event(db_session, ev).vcpu_total
    for _ in range(3):
        again = forfeit.tick(db_session, now=DUE + timedelta(minutes=5))
        assert again.forfeited == [] and again.mails == [] and again.invites == []
    assert (_event(db_session, ev).sequence, _event(db_session, ev).vcpu_total) == (seq, vcpu)
    assert len(first.forfeited[0].emails) == 2
    assert len(_audits(db_session, world, "roll_call_forfeit")) == 1


def test_a_replica_with_a_stale_view_loses_the_claim(client, db_session, world, seats):
    """Replica B read the entry as invited before replica A forfeited it: B's guarded update
    matches nothing, so B frees no seat and tells no one."""
    ev = _book(client, world)
    stale = _entry(db_session, "newbie@corp.test")
    forfeit.tick(db_session, now=DUE + SECOND)  # replica A
    vcpu = _event(db_session, ev).vcpu_total
    assert forfeit._claim(db_session, stale, DUE + timedelta(minutes=1)) is False
    assert forfeit.forfeit_due(db_session, _cls(db_session, world), DUE + timedelta(minutes=1)) is None
    assert _event(db_session, ev).vcpu_total == vcpu


def test_a_sign_in_that_wins_the_race_keeps_the_seat(client, db_session, world):
    """The clock read Newbie as invited; she signed in before its guarded update ran."""
    _book(client, world)
    stale = _entry(db_session, "newbie@corp.test")
    stale_status = stale.status
    db_session.query(RollCallEntry).filter(RollCallEntry.id == stale.id).update(
        {RollCallEntry.status: "signed_in"}, synchronize_session=False)
    assert stale_status == "invited"
    assert forfeit._claim(db_session, stale, DUE + SECOND) is False
    assert _entry(db_session, "newbie@corp.test").status == "signed_in"


# ── What forfeiting does ───────────────────────────────────────────────────


def test_the_seat_is_freed_with_a_cancel_and_a_sequence_bump(client, db_session, world, seats):
    ev = _book(client, world, vcpu=40)
    later = _book(client, world, start=NINE + timedelta(days=1), vcpu=40)
    before = {e["id"]: _event(db_session, e).sequence for e in (ev, later)}
    res = forfeit.tick(db_session, now=DUE + SECOND)
    for e in (ev, later):
        row = _event(db_session, e)
        assert row.vcpu_total == 20  # two seats of 10 freed
        assert row.sequence == before[e["id"]] + 1
    assert {(i.to, i.method) for i in res.invites} == {
        (who, "CANCEL") for who in ("newbie@corp.test", "rosie@corp.test")
    } and len(res.invites) == 4  # each of the two sessions, to each of the two
    assert all(f"SEQUENCE:{_event(db_session, ev).sequence}" in i.calendar or
               f"SEQUENCE:{_event(db_session, later).sequence}" in i.calendar for i in res.invites)
    # Removing a forfeited Student later frees nothing a second time.
    act_as(world["lead"])
    newbie = _entry(db_session, "newbie@corp.test")
    assert client.delete(f"/classes/{world['class']['id']}/roll-call/{newbie.id}").status_code == 204
    assert _event(db_session, ev).vcpu_total == 20


def test_forfeiting_touches_this_roll_call_only(client, db_session, world):
    rosie = world["rosie"]
    other = Course(id=uuid.uuid4(), name="Network Defence", tenant_id=world["a"].id, is_published=True)
    db_session.add(other)
    db_session.flush()
    # Rosie is also on another class of the tenant, which has no approved session yet.
    k2 = _class(client, world, world["lead"], name="ND-1", course=other)
    act_as(world["lead"])
    client.post(f"/classes/{k2['id']}/roll-call", json={"emails": ["rosie@corp.test"]})
    enrolments = db_session.query(Enrollment).filter(Enrollment.user_id == rosie.id).count()
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    db_session.refresh(rosie)
    assert rosie.is_active and rosie.deleted_at is None and rosie.role == UserRole.student
    assert db_session.query(Enrollment).filter(Enrollment.user_id == rosie.id).count() == enrolments == 2
    other_entry = db_session.query(RollCallEntry).filter(RollCallEntry.class_id == uuid.UUID(k2["id"])).one()
    assert other_entry.status == "invited" and other_entry.forfeited_at is None


def test_the_lead_and_assistants_are_told_once_outside_the_email_budget(client, db_session, world, outbox):
    _book(client, world)
    outbox.clear()
    budget = class_service.emails_sent_today(db_session, uuid.UUID(world["lead"].id))
    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert {m.to for m in res.mails} == {world["lead"].email, world["assistant"].email}
    assert len(res.mails) == 2
    body = res.mails[0].body
    assert "newbie@corp.test" in body and "rosie@corp.test" in body and "ann@corp.test" not in body
    assert "Reinstate" in body and "C201-26-03" in res.mails[0].subject
    asyncio.run(class_service.send(res.mails))
    assert {m["to"] for m in outbox.sent} == {world["lead"].email, world["assistant"].email}
    # Observers and other instructors are not told; the instructors' daily budget is untouched.
    assert world["observer"].email not in outbox.to() and world["other_instructor"].email not in outbox.to()
    assert class_service.emails_sent_today(db_session, uuid.UUID(world["lead"].id)) == budget


def test_an_inactive_assistant_is_not_mailed(client, db_session, world):
    db_session.get(User, uuid.UUID(world["assistant"].id)).is_active = False
    db_session.flush()
    _book(client, world)
    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert {m.to for m in res.mails} == {world["lead"].email}


def test_forfeiting_is_audited(client, db_session, world):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    (row,) = _audits(db_session, world, "roll_call_forfeit")
    assert row.tenant_id == world["a"].id and row.user_id is None and row.resource_type == "class"
    assert "newbie@corp.test" in row.detail and "rosie@corp.test" in row.detail and "ann@" not in row.detail


def test_the_roll_call_shows_forfeited(client, db_session, world):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    act_as(world["assistant"])
    rows = {e["email"]: e for e in client.get(f"/classes/{world['class']['id']}/roll-call").json()}
    assert rows["newbie@corp.test"]["status"] == "forfeited" and rows["newbie@corp.test"]["forfeited_at"]
    assert rows["ann@corp.test"]["forfeited_at"] is None
    # Off the roll call: the class's count and its invites leave them out.
    assert client.get(f"/classes/{world['class']['id']}").json()["roll_call_count"] == 1
    assert class_service.class_roll_call_emails(db_session, _cls(db_session, world)) == ["ann@corp.test"]
    # Adding the address again is refused: Reinstate is the way back.
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["newbie@corp.test"]})
    assert r.json()["added"] == [] and "reinstate" in r.json()["refused"][0]["reason"]


# ── Admission ──────────────────────────────────────────────────────────────


def test_admission_is_refused_through_a_forfeited_entry(client, db_session, world, sign_in):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    me = sign_in(_identity("newbie@corp.test", "ad-newbie"))
    assert me["status"] == "unregistered"
    assert me["roll_call_forfeited"] == ["C201-26-03"]
    assert db_session.query(User).filter(User.keycloak_id == "ad-newbie").count() == 0
    assert _entry(db_session, "newbie@corp.test").status == "forfeited"
    # The roster row the same: never adopted through its forfeited entry.
    me = sign_in(_identity("rosie@corp.test", "ad-rosie"))
    assert me["status"] == "unregistered" and me["roll_call_forfeited"] == ["C201-26-03"]
    db_session.refresh(world["rosie"])
    assert world["rosie"].source == "csv_import"


def test_another_class_still_admits_them(client, db_session, world, sign_in):
    other = Course(id=uuid.uuid4(), name="Network Defence", tenant_id=world["a"].id, is_published=True)
    db_session.add(other)
    db_session.flush()
    k2 = _class(client, world, world["lead"], name="ND-1", course=other)
    act_as(world["lead"])
    client.post(f"/classes/{k2['id']}/roll-call", json={"emails": ["newbie@corp.test"]})
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    me = sign_in(_identity("newbie@corp.test", "ad-newbie"))
    assert me["status"] == "registered" and me["roll_call_forfeited"] == ["C201-26-03"]
    user = db_session.query(User).filter(User.keycloak_id == "ad-newbie").one()
    assert {c for (c,) in db_session.query(Enrollment.course_id).filter(Enrollment.user_id == user.id)} == {other.id}
    assert _entry(db_session, "newbie@corp.test", world).status == "forfeited"


def test_an_unverified_email_is_not_told_about_forfeits(client, db_session, world, sign_in):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    me = sign_in(_identity("newbie@corp.test", "ad-newbie", email_verified=False))
    assert me["roll_call_forfeited"] == []


# ── Reinstating ────────────────────────────────────────────────────────────


def _reinstate(client, w, email, db, who=None):
    act_as(who or w["lead"])
    return client.post(f"/classes/{w['class']['id']}/roll-call/{_entry(db, email, w).id}/reinstate")


def test_reinstate_when_it_fits(client, db_session, world, seats, outbox):
    ev = _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    seq = _event(db_session, ev).sequence
    outbox.clear()
    r = _reinstate(client, world, "newbie@corp.test", db_session)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "invited" and r.json()["pending_reason"] is None
    assert _event(db_session, ev).vcpu_total == 30  # 40 - 2 seats + 1
    assert _event(db_session, ev).sequence == seq + 1
    assert outbox.to("REQUEST") == {"newbie@corp.test"}
    entry = _entry(db_session, "newbie@corp.test")
    assert entry.forfeited_at is not None  # kept: the clock never forfeits them again
    assert forfeit._claim(db_session, entry, DUE + timedelta(hours=6)) is False  # even a stale replica
    forfeit.tick(db_session, now=DUE + timedelta(hours=6))
    assert _entry(db_session, "newbie@corp.test").status == "invited"
    (row,) = _audits(db_session, world, "roll_call_reinstate")
    assert str(row.user_id) == world["lead"].id and "newbie@corp.test" in row.detail
    # Once only: a second click is a conflict, not a second seat.
    assert _reinstate(client, world, "newbie@corp.test", db_session).status_code == 409
    assert _event(db_session, ev).vcpu_total == 30


def test_reinstate_is_signed_in_when_they_have_signed_in_since(client, db_session, world):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    rosie = world["rosie"]
    rosie.source, rosie.keycloak_id = "ad", "ad-rosie"  # adopted since, through Request access
    db_session.flush()
    r = _reinstate(client, world, "rosie@corp.test", db_session)
    assert r.status_code == 200 and r.json()["status"] == "signed_in"
    entry = _entry(db_session, "rosie@corp.test")
    assert entry.user_id == rosie.id and entry.signed_in_at is not None


def test_reinstate_without_room_waits_for_an_approver(client, db_session, world, seats, outbox):
    ev = _book(client, world, vcpu=40)
    forfeit.tick(db_session, now=DUE + SECOND)  # 20 vCPU left on the session
    db_session.add(ScheduledEvent(name="someone else", state=EventState.scheduled, tenant_id=world["b"].id,
                                  start_time=NINE, end_time=NINE + timedelta(hours=3), vcpu_total=75))
    db_session.flush()  # 5 vCPU free: no room for a 10-vCPU seat
    outbox.clear()
    r = _reinstate(client, world, "newbie@corp.test", db_session)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert "vCPU" in r.json()["pending_reason"] and "reinstated" in r.json()["pending_reason"]
    assert _event(db_session, ev).vcpu_total == 20 and outbox.sent == []
    assert len(_audits(db_session, world, "roll_call_reinstate_pending")) == 1
    act_as(world["range_ops"])
    entry = _entry(db_session, "newbie@corp.test")
    assert [p["entry_id"] for p in client.get("/classes/pending-additions").json()] == [str(entry.id)]
    # Room again: the approver lets them back on, and the clock still leaves them alone.
    db_session.query(ScheduledEvent).filter(ScheduledEvent.name == "someone else").delete()
    db_session.flush()
    r = client.post(f"/classes/{world['class']['id']}/roll-call/{entry.id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "invited"
    assert _event(db_session, ev).vcpu_total == 30
    forfeit.tick(db_session, now=DUE + timedelta(hours=6))
    assert _entry(db_session, "newbie@corp.test").status == "invited"


@pytest.mark.parametrize(
    ("who", "status"),
    [("lead", 200), ("admin", 200), ("assistant", 403), ("observer", 403), ("other_instructor", 404),
     ("range_ops", 403), ("b_admin", 404), ("b_instructor", 404)],
)
def test_only_the_lead_or_an_admin_reinstates(client, db_session, world, who, status):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    r = _reinstate(client, world, "newbie@corp.test", db_session, who=world[who])
    assert r.status_code == status, r.text
    assert _entry(db_session, "newbie@corp.test").status == ("invited" if status == 200 else "forfeited")


def test_a_student_cannot_reinstate_themselves(client, db_session, world):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    r = _reinstate(client, world, "newbie@corp.test", db_session, who=class_service_user(world["ann"]))
    assert r.status_code == 403


def test_only_a_forfeited_entry_is_reinstated(client, db_session, world):
    _book(client, world)
    assert _reinstate(client, world, "newbie@corp.test", db_session).status_code == 409
    assert _reinstate(client, world, "ann@corp.test", db_session).status_code == 409


# ── Tenants ────────────────────────────────────────────────────────────────


def test_tenants_are_kept_apart(client, db_session, world, sign_in):
    _book(client, world)
    # Tenant B has a class whose session is approved far later: not due.
    act_as(world["b_instructor"])
    kb = client.post("/classes", json={
        "name": "B-1", "course_id": str(world["b_course"].id), "start_date": str(DAY.date()),
        "end_date": str((DAY + timedelta(days=4)).date()), "assistant_ids": [], "observer_ids": [],
    }).json()
    client.post(f"/classes/{kb['id']}/roll-call", json={"emails": ["newbie@corp.test"]})
    act_as(world["b_instructor"])
    r = client.post("/schedule/events", json={
        "name": "B lab", "start_time": (NINE + timedelta(days=2)).isoformat(),
        "end_time": (NINE + timedelta(days=2, hours=3)).isoformat(), "class_id": kb["id"], "vcpu_total": 10,
    })
    act_as(world["b_admin"])
    assert client.post(f"/schedule/events/{r.json()['id']}/approve").status_code == 200

    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert [str(f.class_id) for f in res.forfeited] == [world["class"]["id"]]
    b_entry = db_session.query(RollCallEntry).filter(RollCallEntry.class_id == uuid.UUID(kb["id"])).one()
    assert b_entry.status == "invited"
    assert all(m.to not in (world["b_admin"].email, world["b_instructor"].email) for m in res.mails)
    assert {a.tenant_id for a in db_session.query(AuditLog).filter(AuditLog.action == "roll_call_forfeit")} == {
        world["a"].id}
    # B's admin cannot see or reinstate A's entry.
    a_entry = _entry(db_session, "newbie@corp.test", world)
    act_as(world["b_admin"])
    assert client.post(
        f"/classes/{world['class']['id']}/roll-call/{a_entry.id}/reinstate").status_code == 404
    assert client.post(f"/classes/{kb['id']}/roll-call/{a_entry.id}/reinstate").status_code == 404
    # Tenant B's roll call still admits them (A's forfeited entry is no match), and as B's
    # Student they hear nothing of A's class.
    me = sign_in(_identity("newbie@corp.test", "ad-newbie"))
    assert me["status"] == "registered" and me["roll_call_forfeited"] == []
    assert db_session.query(User).filter(User.keycloak_id == "ad-newbie").one().tenant_id == world["b"].id
    assert _entry(db_session, "newbie@corp.test", world).status == "forfeited"


def test_signing_in_never_signs_in_another_tenants_entry(client, db_session, world, sign_in):
    """An entry linked to an account but filed under another tenant (it cannot happen through
    the API) is never signed in by that account."""
    ann = world["ann"]
    act_as(world["b_instructor"])
    kb = client.post("/classes", json={
        "name": "B-1", "course_id": str(world["b_course"].id), "start_date": str(DAY.date()),
        "end_date": str((DAY + timedelta(days=4)).date()), "assistant_ids": [], "observer_ids": [],
    }).json()
    stray = RollCallEntry(class_id=uuid.UUID(kb["id"]), tenant_id=world["b"].id, email="ann@corp.test",
                          user_id=ann.id, status="invited")
    db_session.add(stray)
    db_session.flush()
    assert forfeit.note_sign_in(db_session, ann) == 0
    db_session.refresh(stray)
    assert stray.status == "invited"


# ── Second review (adversarial pass on #280) ───────────────────────────────


def test_r1_an_adopted_entry_the_student_never_refreshed_is_not_forfeited(client, db_session, world):
    """Rosie's roster row was adopted (Request access) before this rule existed; her entry is
    still invited and she has not called /auth/me since. The clock counts her as signed in."""
    rosie = world["rosie"]
    rosie.source, rosie.keycloak_id = "ad", "ad-rosie"
    db_session.flush()
    _book(client, world)
    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert [f.emails for f in res.forfeited] == [["newbie@corp.test"]]
    entry = _entry(db_session, "rosie@corp.test")
    assert entry.status == "signed_in" and entry.signed_in_at is not None and entry.forfeited_at is None
    assert len(_audits(db_session, world, "roll_call_signed_in")) == 1


def test_r1_a_staff_account_is_no_students_sign_in(client, db_session, world):
    rosie = world["rosie"]
    rosie.source, rosie.role = "ad", UserRole.instructor
    db_session.flush()
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    assert _entry(db_session, "rosie@corp.test").status == "forfeited"


class _FailingCommit:
    """The session as a replica sees it when its commit fails (a deadlock, a lost connection)."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        raise RuntimeError("connection lost")

    def rollback(self):
        self._db.expire_all()


def test_r2_a_failed_commit_sends_no_cancel_and_no_notice(client, db_session, world):
    _book(client, world)
    res = forfeit.tick(_FailingCommit(db_session), now=DUE + SECOND)
    assert res.forfeited == [] and res.mails == [] and res.invites == []


def test_r3_a_removal_racing_the_clock_frees_nothing_twice(client, db_session, world, seats):
    ev = _book(client, world)
    stale = _entry(db_session, "newbie@corp.test")  # the lead's request read it as invited
    forfeit.tick(db_session, now=DUE + SECOND)
    vcpu = _event(db_session, ev).vcpu_total
    set_committed_value(stale, "status", "invited")
    with pytest.raises(HTTPException) as exc:
        class_service.remove_from_roll_call(db_session, _cls(db_session, world), stale, world["lead"])
    assert exc.value.status_code == 409
    assert _event(db_session, ev).vcpu_total == vcpu


def test_r4_two_reinstatements_at_once_take_one_seat(client, db_session, world, seats):
    ev = _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    entry = _entry(db_session, "newbie@corp.test")
    cls = _cls(db_session, world)
    forfeit.reinstate(db_session, cls, entry, world["lead"])
    db_session.flush()
    vcpu, seq = _event(db_session, ev).vcpu_total, _event(db_session, ev).sequence
    set_committed_value(entry, "status", "forfeited")  # the admin's request read it before the lead's landed
    with pytest.raises(HTTPException) as exc:
        forfeit.reinstate(db_session, cls, entry, world["admin"])
    assert exc.value.status_code == 409
    assert (_event(db_session, ev).vcpu_total, _event(db_session, ev).sequence) == (vcpu, seq)


def test_r6_reinstate_rechecks_the_domain_allow_list(client, db_session, world):
    _book(client, world)
    forfeit.tick(db_session, now=DUE + SECOND)
    db_session.get(RollCallSettings, world["a"].id).email_domains = ["other.test"]
    db_session.flush()
    r = _reinstate(client, world, "newbie@corp.test", db_session)
    assert r.status_code == 422 and "corp.test" in r.json()["detail"]
    assert _entry(db_session, "newbie@corp.test").status == "forfeited"


def test_r7_with_no_active_lead_or_assistant_the_admins_are_told(client, db_session, world):
    for who in ("lead", "assistant"):
        db_session.get(User, uuid.UUID(world[who].id)).is_active = False
    db_session.flush()
    _book(client, world)
    res = forfeit.tick(db_session, now=DUE + SECOND)
    assert {m.to for m in res.mails} == {world["admin"].email}


def test_r8_a_remove_that_loses_a_lock_race_is_409_not_500(client, db_session, world, monkeypatch):
    from sqlalchemy.exc import OperationalError

    class DeadlockError(Exception):
        sqlstate = "40P01"

    def lose(*a, **k):
        raise OperationalError("UPDATE class_roll_call", {}, DeadlockError())

    monkeypatch.setattr(class_service, "remove_from_roll_call", lose)
    entry = _entry(db_session, "newbie@corp.test")
    act_as(world["lead"])
    r = client.delete(f"/classes/{world['class']['id']}/roll-call/{entry.id}")
    assert r.status_code == 409 and "try again" in r.json()["detail"]


def test_r9_no_day_before_reminder_for_an_entry_whose_account_has_signed_in(db_session, world):
    from app.classes import clock as reminder_clock

    rosie = world["rosie"]
    rosie.source = "ad"  # adopted through Request access; her entry still says invited
    db_session.flush()
    eve = datetime.combine(DAY.date() - timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    assert {m.to for m in reminder_clock.tick(db_session, now=eve)} == {"newbie@corp.test"}
