"""Classes 7: a Student's "My classes" page (ADR 0014 §20; #284).

``GET /classes/attending`` (app/classes/my_classes.py). The rules under test:

- **class:attend**: Students hold it; observers, instructors and range ops do not (403).
- Only the caller's own roll-call entries, in their own tenant: never another tenant's class
  (even with the same user id on a roll call there), never a class they were removed from
  or whose addition awaits an approver.
- Per class: name, course, the lead's name with no email anywhere in the answer, dates,
  sessions in the window (approved or over; never drafts, pending, rejected or cancelled).
- Seat: ready, or forfeited with the deadline missed; a forfeited seat shows no range and
  no Join.
- Range for the current or next session: not built, building, ready or failed. Never
  another Student's seat, never why a build failed.
- Join (``join_range_id``) only when the credentials endpoint hands them over: their own
  seat ready, or the shared range ready during a collective session under way. The
  credentials endpoint agrees on every case here.
- The window: default a week, at most 45 days, end after start.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.acknowledgement.models import AcknowledgementSignature, AcknowledgementVersion
from app.auth import CurrentUser
from app.classes.models import Class, ClassRange, ClassSession, RollCallEntry
from app.models import Course, Range, RangeState, Template, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent

NOW = datetime.now(UTC).replace(microsecond=0)
URL = "/classes/attending"


def _student(db, tenant_id, name: str) -> CurrentUser:
    uid = uuid.uuid4()
    email = f"{name}@corp.test"
    db.add(User(id=uid, keycloak_id=f"kc-{uid}", email=email, display_name=name.title(), role=UserRole.student,
                tenant_id=tenant_id, source="ad"))
    db.flush()
    return CurrentUser(id=str(uid), email=email, display_name=name.title(), role=UserRole.student,
                       tenant_id=str(tenant_id), keycloak_id=f"kc-{uid}")


def _class(db, tenant_id, course, lead, name="C201-26-03", days=(-1, 4), status="open") -> Class:
    c = Class(tenant_id=tenant_id, name=name, course_id=course.id, lead_instructor_id=uuid.UUID(lead.id),
              start_date=(NOW + timedelta(days=days[0])).date(), end_date=(NOW + timedelta(days=days[1])).date(),
              status=status)
    db.add(c)
    db.flush()
    return c


def _entry(db, cls, who: CurrentUser, status="signed_in", **kw) -> RollCallEntry:
    e = RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email=who.email, user_id=uuid.UUID(who.id),
                      status=status, signed_in_at=NOW if status == "signed_in" else None, **kw)
    db.add(e)
    db.flush()
    return e


def _session(db, cls, template, start, hours=3, mode="individual", state=EventState.scheduled, name="Lab day",
             range_id=None) -> ScheduledEvent:
    evt = ScheduledEvent(name=name, state=state, tenant_id=cls.tenant_id, template_id=template.id,
                         start_time=start, end_time=start + timedelta(hours=hours), range_id=range_id)
    db.add(evt)
    db.flush()
    db.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=cls.tenant_id, range_mode=mode))
    db.flush()
    return evt


def _range(db, cls, template, entry=None, state=RangeState.ready, last_error=None) -> ClassRange:
    rng = Range(name=f"{cls.name} range", template_id=template.id, tenant_id=cls.tenant_id, state=state)
    db.add(rng)
    db.flush()
    row = ClassRange(tenant_id=cls.tenant_id, class_id=cls.id, template_id=template.id,
                     kind="seat" if entry else "shared", entry_id=entry.id if entry else None,
                     seat_key=str(entry.id) if entry else "shared", range_id=rng.id, builds=1, built_at=NOW,
                     last_error=last_error)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def world(db_session):
    db = db_session
    a, b = real_tenant(db, "mine-a"), real_tenant(db, "mine-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    b_course = Course(id=uuid.uuid4(), name="B course", tenant_id=b.id, is_published=True)
    tpl = Template(id=uuid.uuid4(), name="Analyst seat", yaml="nodes: []\n", tenant_id=a.id)
    shared_tpl = Template(id=uuid.uuid4(), name="Exercise range", yaml="nodes: []\n", tenant_id=a.id)
    b_tpl = Template(id=uuid.uuid4(), name="B seat", yaml="nodes: []\n", tenant_id=b.id)
    db.add_all([course, b_course, tpl, shared_tpl, b_tpl])
    db.flush()
    lead = real_user(db, UserRole.instructor, a.id)
    w = {
        "a": a, "b": b, "course": course, "tpl": tpl, "shared_tpl": shared_tpl, "b_tpl": b_tpl, "lead": lead,
        "observer": real_user(db, UserRole.observer, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "range_ops": real_user(db, UserRole.range_ops, a.id),
        "b_lead": real_user(db, UserRole.instructor, b.id),
        "ann": _student(db, a.id, "ann"),
        "bob": _student(db, a.id, "bob"),
    }
    cls = _class(db, a.id, course, lead)
    w["cls"] = cls
    w["ann_entry"] = _entry(db, cls, w["ann"])
    w["bob_entry"] = _entry(db, cls, w["bob"])
    db.commit()
    return w


def _get(client, who, **params):
    act_as(who)
    return client.get(URL, params=params)


def _only(body) -> dict:
    assert len(body["classes"]) == 1, body
    return body["classes"][0]


# ── Who may ask ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["observer", "range_ops"])
def test_observers_and_range_ops_do_not_hold_class_attend(client, world, role):
    assert _get(client, world[role]).status_code == 403


def test_an_instructor_holds_it_to_grant_the_student_role_and_sees_only_their_own_entries(client, world):
    # grant_refusal: an instructor approving a Student must hold every Student permission.
    from app.rbac import grant_refusal

    assert grant_refusal(world["lead"], UserRole.student) is None
    r = _get(client, world["lead"])
    assert r.status_code == 200 and r.json()["classes"] == []  # they lead it; they are not on its roll call


def test_a_student_sees_their_class_with_the_leads_name_and_no_email(client, world):
    r = _get(client, world["ann"])
    assert r.status_code == 200, r.text
    c = _only(r.json())
    assert c["name"] == "C201-26-03" and c["course_name"] == "SOC Analyst"
    assert c["lead_instructor"] == "Test instructor"
    assert c["seat"] == "ready" and c["onboarded"] is True
    assert "@" not in r.text  # no email at all: not the lead's, not the Student's, not Bob's
    assert "Bob" not in r.text


def test_an_admin_with_no_roll_call_entry_sees_nothing(client, world):
    r = _get(client, world["admin"])
    assert r.status_code == 200 and r.json()["classes"] == []


# ── Tenant isolation and other Students ─────────────────────────────────────


def test_another_tenants_class_is_never_shown(client, world, db_session):
    db = db_session
    # A row in tenant B naming Ann's own user id (a forged or migrated row): never read.
    b_cls = _class(db, world["b"].id, db.get(Course, world["course"].id), world["b_lead"], name="B-CLASS")
    b_cls.course_id = db.query(Course).filter(Course.tenant_id == world["b"].id).one().id
    e = RollCallEntry(class_id=b_cls.id, tenant_id=world["b"].id, email="ann@corp.test",
                      user_id=uuid.UUID(world["ann"].id), status="signed_in")
    db.add(e)
    db.commit()
    r = _get(client, world["ann"])
    assert [c["name"] for c in r.json()["classes"]] == ["C201-26-03"]
    assert "B-CLASS" not in r.text


def test_removed_pending_and_cancelled_are_not_my_classes(client, world, db_session):
    db = db_session
    other = _class(db, world["a"].id, world["course"], world["lead"], name="REMOVED")
    _entry(db, other, world["ann"], status="removed")
    waiting = _class(db, world["a"].id, world["course"], world["lead"], name="PENDING")
    _entry(db, waiting, world["ann"], status="pending")
    gone = _class(db, world["a"].id, world["course"], world["lead"], name="CANCELLED", status="cancelled")
    _entry(db, gone, world["ann"])
    db.commit()
    assert [c["name"] for c in _get(client, world["ann"]).json()["classes"]] == ["C201-26-03"]


def test_another_students_seat_is_never_shown(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    _range(db, world["cls"], world["tpl"], world["bob_entry"], state=RangeState.ready)
    db.commit()
    r = _get(client, world["ann"])
    c = _only(r.json())
    # Ann's seat is not built yet; Bob's ready one is not hers.
    assert c["range_status"] == "not_built" and c["join_range_id"] is None
    assert str(world["bob_entry"].id) not in r.text


# ── Sessions and the window ─────────────────────────────────────────────────


def test_sessions_in_the_window_only_approved_or_over(client, world, db_session):
    db = db_session
    cls, tpl = world["cls"], world["tpl"]
    _session(db, cls, tpl, NOW + timedelta(days=1), name="Tomorrow")
    _session(db, cls, tpl, NOW - timedelta(hours=6), name="Earlier", state=EventState.completed)
    _session(db, cls, tpl, NOW + timedelta(days=2), name="Draft", state=EventState.draft)
    _session(db, cls, tpl, NOW + timedelta(days=2), name="Waiting", state=EventState.pending_approval)
    _session(db, cls, tpl, NOW + timedelta(days=2), name="Rejected", state=EventState.rejected)
    _session(db, cls, tpl, NOW + timedelta(days=2), name="Off", state=EventState.cancelled)
    _session(db, cls, tpl, NOW + timedelta(days=3), name="Exercise", mode="collective")
    db.commit()
    start = (NOW - timedelta(days=1)).isoformat()
    c = _only(_get(client, world["ann"], start=start).json())
    assert [s["name"] for s in c["sessions"]] == ["Earlier", "Tomorrow", "Exercise"]
    assert [s["range_mode"] for s in c["sessions"]] == ["individual", "individual", "collective"]
    # A narrower window: tomorrow only.
    c = _only(_get(client, world["ann"], start=(NOW + timedelta(hours=12)).isoformat(),
                    end=(NOW + timedelta(days=1, hours=12)).isoformat()).json())
    assert [s["name"] for s in c["sessions"]] == ["Tomorrow"]


def test_a_class_outside_the_window_is_not_listed(client, world, db_session):
    db = db_session
    later = _class(db, world["a"].id, world["course"], world["lead"], name="NEXT-MONTH", days=(30, 34))
    _entry(db, later, world["ann"])
    db.commit()
    assert [c["name"] for c in _get(client, world["ann"]).json()["classes"]] == ["C201-26-03"]
    month = _get(client, world["ann"], start=NOW.isoformat(), end=(NOW + timedelta(days=40)).isoformat()).json()
    assert [c["name"] for c in month["classes"]] == ["C201-26-03", "NEXT-MONTH"]


@pytest.mark.parametrize("days", [-1, 0, 46])
def test_the_window_is_checked(client, world, days):
    end = (NOW + timedelta(days=days)).isoformat()
    assert _get(client, world["ann"], start=NOW.isoformat(), end=end).status_code == 422


# ── Range status and Join ───────────────────────────────────────────────────


@pytest.mark.parametrize(("state", "error", "want"), [
    (RangeState.created, None, "not_built"),
    (RangeState.provisioning, None, "building"),
    (RangeState.ready, None, "ready"),
    (RangeState.failed, None, "failed"),
    (RangeState.created, "Build could not be started: vCenter said no", "failed"),
])
def test_own_seat_status(client, world, db_session, state, error, want):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    row = _range(db, world["cls"], world["tpl"], world["ann_entry"], state=state, last_error=error)
    db.commit()
    r = _get(client, world["ann"])
    c = _only(r.json())
    assert c["range_status"] == want
    assert c["sessions"][0]["range_status"] == want
    assert c["join_range_id"] == (str(row.range_id) if want == "ready" else None)
    assert "vCenter" not in r.text  # why a build failed is for staff


def test_join_opens_only_what_the_credentials_endpoint_hands_over(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    mine = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    bobs = _range(db, world["cls"], world["tpl"], world["bob_entry"])
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["join_range_id"] == str(mine.range_id)
    act_as(world["ann"])
    assert client.get(f"/ranges/{mine.range_id}/credentials").status_code == 200
    assert client.get(f"/ranges/{bobs.range_id}/credentials").status_code == 404


def test_a_shared_range_joins_only_while_a_collective_session_is_under_way(client, world, db_session):
    db = db_session
    cls = world["cls"]
    row = _range(db, cls, world["shared_tpl"])
    evt = _session(db, cls, world["shared_tpl"], NOW + timedelta(hours=1), mode="collective", range_id=row.range_id)
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["range_status"] == "ready" and c["join_range_id"] is None  # ready, but not started
    act_as(world["ann"])
    assert client.get(f"/ranges/{row.range_id}/credentials").status_code == 404

    evt.start_time, evt.state = NOW - timedelta(minutes=10), EventState.active
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["sessions"][0]["live"] is True
    assert c["join_range_id"] == str(row.range_id)
    act_as(world["ann"])
    assert client.get(f"/ranges/{row.range_id}/credentials").status_code == 200


def test_the_current_session_wins_over_the_next(client, world, db_session):
    db = db_session
    cls = world["cls"]
    now_evt = _session(db, cls, world["tpl"], NOW - timedelta(hours=1), state=EventState.active, name="Now")
    _session(db, cls, world["tpl"], NOW + timedelta(days=1), name="Next")
    _range(db, cls, world["tpl"], world["ann_entry"])
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["next_session_id"] == str(now_evt.id)


# ── Forfeit ─────────────────────────────────────────────────────────────────


def test_a_forfeited_seat_says_so_and_offers_no_range(client, world, db_session):
    db = db_session
    cls = world["cls"]
    first = NOW - timedelta(hours=5)
    _session(db, cls, world["tpl"], first, hours=8, state=EventState.active)
    _range(db, cls, world["tpl"], world["ann_entry"])  # built before the forfeit, still standing
    entry = world["ann_entry"]
    entry.status, entry.forfeited_at, entry.signed_in_at = "forfeited", first + timedelta(hours=2), None
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["seat"] == "forfeited" and c["onboarded"] is False
    assert c["forfeit_deadline"] is not None
    assert datetime.fromisoformat(c["forfeit_deadline"]) == first + timedelta(hours=2)
    assert c["range_status"] is None and c["join_range_id"] is None
    assert all(s["range_status"] is None for s in c["sessions"])


# ── Acknowledgement ─────────────────────────────────────────────────────────


def test_acknowledgement_status(client, world, db_session):
    db = db_session
    v = AcknowledgementVersion(version=1, text="Rules", published_at=NOW - timedelta(days=1))
    db.add(v)
    db.flush()
    db.add(AcknowledgementSignature(user_id=uuid.UUID(world["ann"].id), tenant_id=world["a"].id, version_id=v.id,
                                    signed_at=NOW - timedelta(hours=1)))
    db.commit()
    assert _get(client, world["ann"]).json()["acknowledgement_signed"] is True
    db.add(AcknowledgementVersion(version=2, text="Rules v2", published_at=NOW))
    db.commit()
    # A new version: not signed yet. (In the app the sign-in gate sends them to sign first.)
    assert _get(client, world["ann"]).json()["acknowledgement_signed"] is False


# ── Edge cases from review: Join agrees with the credentials endpoint ───────


def test_a_deleted_range_is_not_offered(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    row = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.get(Range, row.range_id).deleted_at = NOW
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["range_status"] == "not_built" and c["join_range_id"] is None
    act_as(world["ann"])
    assert client.get(f"/ranges/{row.range_id}/credentials").status_code == 404


def test_an_exercise_under_way_inside_a_day_long_lab_opens_the_shared_range(client, world, db_session):
    db = db_session
    cls = world["cls"]
    _session(db, cls, world["tpl"], NOW - timedelta(hours=4), hours=8, state=EventState.active, name="All-day lab")
    _range(db, cls, world["tpl"], world["ann_entry"])
    shared = _range(db, cls, world["shared_tpl"])
    ex = _session(db, cls, world["shared_tpl"], NOW - timedelta(minutes=30), hours=1, mode="collective",
                  state=EventState.active, name="Exercise", range_id=shared.range_id)
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["next_session_id"] == str(ex.id) and c["join_range_id"] == str(shared.range_id)
    act_as(world["ann"])
    assert client.get(f"/ranges/{shared.range_id}/credentials").status_code == 200


def test_a_collective_session_is_matched_by_the_range_it_books(client, world, db_session):
    db = db_session
    cls = world["cls"]
    shared = _range(db, cls, world["shared_tpl"])
    # The session's template changed after its shared range was built: it still books that range.
    _session(db, cls, world["tpl"], NOW - timedelta(minutes=10), mode="collective", state=EventState.active,
             range_id=shared.range_id)
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["join_range_id"] == str(shared.range_id)


def test_a_session_with_no_range_is_skipped_for_the_next_that_has_one(client, world, db_session):
    db = db_session
    cls = world["cls"]
    lecture = _session(db, cls, world["tpl"], NOW + timedelta(hours=1), name="Lecture")
    lecture.template_id = None
    lab = _session(db, cls, world["tpl"], NOW + timedelta(days=1), name="Lab")
    seat = _range(db, cls, world["tpl"], world["ann_entry"])
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["sessions"][0]["range_status"] is None  # the lecture uses no range
    assert c["next_session_id"] == str(lab.id) and c["join_range_id"] == str(seat.range_id)


def test_a_last_session_running_past_the_end_date_keeps_the_class(client, world, db_session):
    db = db_session
    late = _class(db, world["a"].id, world["course"], world["lead"], name="ENDED-YESTERDAY", days=(-3, -1))
    entry = _entry(db, late, world["ann"])
    _session(db, late, world["tpl"], NOW - timedelta(hours=1), hours=30, state=EventState.active, name="Overrun")
    seat = _range(db, late, world["tpl"], entry)
    db.commit()
    body = _get(client, world["ann"]).json()
    c = next(c for c in body["classes"] if c["name"] == "ENDED-YESTERDAY")
    assert c["join_range_id"] == str(seat.range_id)


@pytest.mark.parametrize("state", [RangeState.stopping, RangeState.stopped, RangeState.starting])
def test_a_powered_off_seat_is_paused_and_join_waits(client, world, db_session, state):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    _range(db, world["cls"], world["tpl"], world["ann_entry"], state=state)
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["range_status"] == "paused" and c["sessions"][0]["range_status"] == "paused"
    assert c["join_range_id"] is None
    # Staff still see the class range as ready (ranges.status is unchanged).
    act_as(world["lead"])
    staff = client.get(f"/classes/{world['cls'].id}/ranges").json()
    assert {r["status"] for r in staff["ranges"]} == {"ready"}


def test_a_running_seat_is_ready(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(hours=2))
    row = _range(db, world["cls"], world["tpl"], world["ann_entry"], state=RangeState.running)
    db.commit()
    c = _only(_get(client, world["ann"]).json())
    assert c["range_status"] == "ready" and c["join_range_id"] == str(row.range_id)


def test_join_records_attendance(client, world, db_session):
    """Join opens the credentials panel; listing the accounts is the attendance evidence
    (#283, ``classes.attendance.note_range_open``), so the Classes page need not do it."""
    from app.classes.attendance import RANGE_OPEN
    from app.models import AuditLog

    db = db_session
    _session(db, world["cls"], world["tpl"], NOW - timedelta(minutes=20), state=EventState.active)
    row = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.commit()
    join = _only(_get(client, world["ann"]).json())["join_range_id"]
    assert join == str(row.range_id)
    assert client.get(f"/ranges/{join}/credentials").status_code == 200
    opened = db.query(AuditLog).filter(AuditLog.action == RANGE_OPEN, AuditLog.resource_id == join,
                                       AuditLog.user_id == uuid.UUID(world["ann"].id)).count()
    assert opened == 1
