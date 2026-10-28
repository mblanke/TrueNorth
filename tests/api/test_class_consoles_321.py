"""Consoles on class ranges and the observers' live view (ADR 0014 §19-20; #313, #321).

``app/classes/consoles.py`` and ``console_router.py``. The rules under test:

- **A Student's console** (``/classes/attending/ranges/{id}/consoles``): their own seat, or
  the shared range during a collective session under way, by the credentials' own rule
  (``ranges.student_participation``); never another Student's seat, another class's shared
  range (a team or class they are not on), another tenant's range, a class they were taken
  off; only the machines they are handed accounts on; only while a session of the class
  using that range is live or within its provisioning lead.
- **The live view** (``/classes/{id}/sessions/{event_id}/live``): the class's lead,
  assistants, observers (its observer list) and admins only; another tenant's class, or an
  observer not on this class's list, is 404; a Student is 403. Consoles from it during the
  session window only, on a range of that session; an observer's is view-only where the
  hypervisor enforces it, else marked observed.
- **Every ticket is audited**: who, which VM, why (student, observer, staff).
- **A Student's console is attendance evidence**, as opening the accounts is.
- **Live Class** (``/ops/exercises/{id}/class``): a class session's exercise only to observers
  on that class's staff.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.auth import CurrentUser
from app.classes.models import Class, ClassRange, ClassSession, ClassStaff, RollCallEntry
from app.console_backends import MockConsole
from app.models import AuditLog, Course, Range, RangeState, Template, User, UserRole
from app.range_credentials.models import RangeCredential
from app.scheduler.models import EventState, ScheduledEvent

NOW = datetime.now(UTC).replace(microsecond=0)
AUDIT = "range_console_open"


def _student(db, tenant_id, name: str) -> CurrentUser:
    uid = uuid.uuid4()
    email = f"{name}@corp.test"
    db.add(User(id=uid, keycloak_id=f"kc-{uid}", email=email, display_name=name.title(), role=UserRole.student,
                tenant_id=tenant_id, source="ad"))
    db.flush()
    return CurrentUser(id=str(uid), email=email, display_name=name.title(), role=UserRole.student,
                       tenant_id=str(tenant_id), keycloak_id=f"kc-{uid}")


def _class(db, tenant_id, course, lead, name="C201-26-03") -> Class:
    c = Class(tenant_id=tenant_id, name=name, course_id=course.id, lead_instructor_id=uuid.UUID(lead.id),
              start_date=(NOW - timedelta(days=1)).date(), end_date=(NOW + timedelta(days=4)).date(), status="open")
    db.add(c)
    db.flush()
    return c


def _entry(db, cls, who: CurrentUser, status="signed_in") -> RollCallEntry:
    e = RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email=who.email, user_id=uuid.UUID(who.id),
                      status=status, signed_in_at=NOW)
    db.add(e)
    db.flush()
    return e


def _session(db, cls, template, start, hours=3, mode="individual", state=EventState.active, range_id=None,
             exercise_id=None) -> ScheduledEvent:
    evt = ScheduledEvent(name="Lab day", state=state, tenant_id=cls.tenant_id, template_id=template.id,
                         start_time=start, end_time=start + timedelta(hours=hours), range_id=range_id,
                         exercise_id=exercise_id)
    db.add(evt)
    db.flush()
    db.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=cls.tenant_id, range_mode=mode))
    db.flush()
    return evt


def _range(db, cls, template, entry=None, state=RangeState.ready) -> ClassRange:
    rid = uuid.uuid4()
    vms = [{"vm_id": f"vm-{rid.hex[:6]}-1", "name": f"{str(rid)[:8]}-ws1", "node_id": "ws1", "status": "running"},
           {"vm_id": f"vm-{rid.hex[:6]}-2", "name": f"{str(rid)[:8]}-dc", "node_id": "dc", "status": "running"}]
    rng = Range(id=rid, name=f"{cls.name} range", template_id=template.id, tenant_id=cls.tenant_id, state=state,
                provisioner_backend="mock", provisioner_output=json.dumps({"vms": vms}))
    db.add(rng)
    db.flush()
    # The Student is handed an account on ws1 only; dc is staff-only.
    db.add_all([
        RangeCredential(tenant_id=cls.tenant_id, range_id=rid, machine="ws1", username="analyst",
                        sealed_password="x", student_visible=True, applied=True),
        RangeCredential(tenant_id=cls.tenant_id, range_id=rid, machine="dc", username="Administrator",
                        sealed_password="x", student_visible=False, applied=True, role="admin"),
    ])
    row = ClassRange(tenant_id=cls.tenant_id, class_id=cls.id, template_id=template.id,
                     kind="seat" if entry else "shared", entry_id=entry.id if entry else None,
                     seat_key=str(entry.id) if entry else "shared", range_id=rng.id, builds=1, built_at=NOW)
    db.add(row)
    db.flush()
    return row


def _staff(db, cls, who: CurrentUser, role: str) -> None:
    db.add(ClassStaff(class_id=cls.id, user_id=uuid.UUID(who.id), role=role))
    db.flush()


@pytest.fixture(autouse=True)
def _mock_console(monkeypatch):
    # Observer consoles are off by default; most tests exercise them on (the setting).
    monkeypatch.setenv("CLASS_OBSERVER_CONSOLES", "on")
    MockConsole.supports_view_only = False
    yield
    MockConsole.supports_view_only = False


@pytest.fixture
def world(db_session):
    db = db_session
    a, b = real_tenant(db, "cons-a"), real_tenant(db, "cons-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    b_course = Course(id=uuid.uuid4(), name="B course", tenant_id=b.id, is_published=True)
    tpl = Template(id=uuid.uuid4(), name="Analyst seat", yaml="nodes: []\n", tenant_id=a.id)
    b_tpl = Template(id=uuid.uuid4(), name="B seat", yaml="nodes: []\n", tenant_id=b.id)
    db.add_all([course, b_course, tpl, b_tpl])
    db.flush()
    lead = real_user(db, UserRole.instructor, a.id)
    w = {
        "a": a, "b": b, "course": course, "b_course": b_course, "tpl": tpl, "b_tpl": b_tpl, "lead": lead,
        "observer": real_user(db, UserRole.observer, a.id),
        "other_observer": real_user(db, UserRole.observer, a.id),
        "b_observer": real_user(db, UserRole.observer, b.id),
        "b_lead": real_user(db, UserRole.instructor, b.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "ann": _student(db, a.id, "ann"),
        "bob": _student(db, a.id, "bob"),
        "cid": _student(db, a.id, "cid"),  # on another class only
    }
    cls = _class(db, a.id, course, lead)
    _staff(db, cls, w["observer"], "observer")
    w["cls"] = cls
    w["ann_entry"] = _entry(db, cls, w["ann"])
    w["bob_entry"] = _entry(db, cls, w["bob"])
    other = _class(db, a.id, course, lead, name="C201-26-04")
    w["other"] = other
    w["cid_entry"] = _entry(db, other, w["cid"])
    db.commit()
    return w


def _individual(db, w, start=None):
    """A live individual session with Ann's and Bob's seats."""
    evt = _session(db, w["cls"], w["tpl"], start or NOW - timedelta(hours=1))
    ann = _range(db, w["cls"], w["tpl"], w["ann_entry"])
    bob = _range(db, w["cls"], w["tpl"], w["bob_entry"])
    db.commit()
    return evt, ann, bob


def _vm(range_id, node: str) -> str:
    """The VM name the fixture records for a node (``<range8>-<node>``)."""
    return f"{str(range_id)[:8]}-{node}"


def _console(client, who, range_id, node="ws1"):
    act_as(who)
    return client.post(f"/classes/attending/ranges/{range_id}/consoles/{_vm(range_id, node)}")


def _audits(db, action=AUDIT):
    return [json.loads(r.detail) | {"_user": str(r.user_id)}
            for r in db.query(AuditLog).filter(AuditLog.action == action).all()]


# ── A Student's console (#313) ───────────────────────────────────────────────


def test_a_student_opens_a_console_on_their_own_seat_during_the_session(client, world, db_session):
    _, ann, _ = _individual(db_session, world)
    r = _console(client, world["ann"], ann.range_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "mock" and body["url"].startswith("mock://console/") and body["machine"] == "ws1"
    assert body["vm"] == _vm(ann.range_id, "ws1")
    assert body["observed"] is False and body["view_only"] is False


def test_a_student_never_gets_another_students_seat(client, world, db_session):
    _, _, bob = _individual(db_session, world)
    assert _console(client, world["ann"], bob.range_id).status_code == 404
    act_as(world["ann"])
    assert client.get(f"/classes/attending/ranges/{bob.range_id}/consoles").status_code == 404
    assert _audits(db_session) == []


def test_a_student_gets_only_the_machines_they_are_handed_accounts_on(client, world, db_session):
    _, ann, _ = _individual(db_session, world)
    act_as(world["ann"])
    listed = client.get(f"/classes/attending/ranges/{ann.range_id}/consoles")
    assert listed.status_code == 200, listed.text
    assert [m["machine"] for m in listed.json()["machines"]] == ["ws1"] and listed.json()["open"] is True
    assert _console(client, world["ann"], ann.range_id, "dc").status_code == 404


def test_no_console_outside_the_session_window(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(days=1))  # tomorrow
    ann = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.commit()
    act_as(world["ann"])
    assert client.get(f"/classes/attending/ranges/{ann.range_id}/consoles").json()["open"] is False
    assert _console(client, world["ann"], ann.range_id).status_code == 403
    assert _audits(db) == []


def test_a_session_that_has_ended_opens_no_console(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=5), hours=2, state=EventState.completed)
    ann = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.commit()
    assert _console(client, world["ann"], ann.range_id).status_code == 403


def test_the_console_opens_within_the_provisioning_lead(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW + timedelta(minutes=10), state=EventState.scheduled)
    ann = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.commit()
    assert _console(client, world["ann"], ann.range_id).status_code == 200


def test_an_unapproved_session_opens_no_console(client, world, db_session):
    db = db_session
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), state=EventState.pending_approval)
    ann = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    db.commit()
    assert _console(client, world["ann"], ann.range_id).status_code == 403


def test_a_student_taken_off_the_roll_call_gets_no_console(client, world, db_session):
    _, ann, _ = _individual(db_session, world)
    world["ann_entry"].status = "removed"
    db_session.commit()
    assert _console(client, world["ann"], ann.range_id).status_code == 404


def test_a_powered_off_seat_opens_no_console(client, world, db_session):
    _, ann, _ = _individual(db_session, world)
    db_session.get(Range, ann.range_id).state = RangeState.stopped
    db_session.commit()
    assert _console(client, world["ann"], ann.range_id).status_code == 409


def test_the_shared_range_during_a_collective_session_and_never_for_a_student_of_another_class(
        client, world, db_session):
    db = db_session
    shared = _range(db, world["cls"], world["tpl"])
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), mode="collective", range_id=shared.range_id)
    db.commit()
    assert _console(client, world["ann"], shared.range_id).status_code == 200
    assert _console(client, world["bob"], shared.range_id).status_code == 200
    # Cid is on another class: not this class's team, not its range.
    assert _console(client, world["cid"], shared.range_id).status_code == 404
    assert _console(client, world["ann"], shared.range_id, "dc").status_code == 404


def test_the_shared_range_opens_no_console_before_its_session_is_under_way(client, world, db_session):
    db = db_session
    shared = _range(db, world["cls"], world["tpl"])
    _session(db, world["cls"], world["tpl"], NOW + timedelta(minutes=10), mode="collective",
             state=EventState.scheduled, range_id=shared.range_id)
    db.commit()
    assert _console(client, world["ann"], shared.range_id).status_code == 404  # as its credentials


def test_another_tenants_range_is_not_found(client, world, db_session):
    db = db_session
    b_cls = _class(db, world["b"].id, world["b_course"], world["b_lead"], name="B-CLASS")
    # A forged entry naming Ann's id in tenant B.
    db.add(RollCallEntry(class_id=b_cls.id, tenant_id=world["b"].id, email="ann@corp.test",
                         user_id=uuid.UUID(world["ann"].id), status="signed_in"))
    db.flush()
    _session(db, b_cls, world["b_tpl"], NOW - timedelta(hours=1))
    b_entry = db.query(RollCallEntry).filter(RollCallEntry.class_id == b_cls.id).one()
    b_seat = _range(db, b_cls, world["b_tpl"], b_entry)
    db.commit()
    assert _console(client, world["ann"], b_seat.range_id).status_code == 404


@pytest.mark.parametrize("role", ["observer", "lead"])
def test_staff_and_observers_do_not_use_the_student_endpoint(client, world, db_session, role):
    _, ann, _ = _individual(db_session, world)
    assert _console(client, world[role], ann.range_id).status_code in (403, 404)


def test_every_student_ticket_is_audited_and_counts_as_attendance(client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    assert _console(client, world["ann"], ann.range_id).status_code == 200
    rows = _audits(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row["_user"] == world["ann"].id and row["as"] == "student"
    assert row["machine"] == "ws1" and row["vm_id"].endswith("-1") and row["range_id"] == str(ann.range_id)
    assert row["class_id"] == str(world["cls"].id) and row["event_id"] == str(evt.id)
    assert row["class_seat_id"] == str(ann.id)
    assert "url" not in row and "ticket" not in json.dumps(row)
    opens = db_session.query(AuditLog).filter(AuditLog.action == "class_range_open").all()
    assert len(opens) == 1 and opens[0].user_id == uuid.UUID(world["ann"].id)
    assert opens[0].resource_id == str(ann.range_id)


def test_the_attendance_clock_counts_a_console_as_present(client, world, db_session):
    from app.classes import attendance

    db = db_session
    evt, ann, _ = _individual(db, world)
    assert _console(client, world["ann"], ann.range_id).status_code == 200
    verdicts = attendance.evaluate(db, world["cls"], evt, [world["ann_entry"], world["bob_entry"]])
    assert verdicts[world["ann_entry"].id][0] == "present"
    assert verdicts[world["bob_entry"].id][0] == "absent"


# ── The live view (ADR 0014 §19) ─────────────────────────────────────────────


def _live(client, who, cls, evt):
    act_as(who)
    return client.get(f"/classes/{cls.id}/sessions/{evt.id}/live")


def _watch(client, who, cls, evt, range_id, node="ws1"):
    act_as(who)
    return client.post(f"/classes/{cls.id}/sessions/{evt.id}/live/console",
                       json={"range_id": str(range_id), "vm": _vm(range_id, node)})


def test_an_observer_of_the_class_sees_every_students_machines(client, world, db_session):
    evt, ann, bob = _individual(db_session, world)
    r = _live(client, world["observer"], world["cls"], evt)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "observer" and body["live"] is True and body["consoles_open"] is True
    seats = {s["display_name"]: s["range"]["range_id"] for s in body["students"]}
    assert seats == {"Ann": str(ann.range_id), "Bob": str(bob.range_id)}
    machines = {m["machine"]: m["student_machine"] for m in body["students"][0]["range"]["machines"]}
    assert machines == {"ws1": True, "dc": False}
    assert "password" not in r.text and "Administrator" not in r.text


def test_an_observer_not_on_the_class_and_another_tenants_get_404(client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    for who in ("other_observer", "b_observer", "b_lead"):
        assert _live(client, world[who], world["cls"], evt).status_code == 404, who
        assert _watch(client, world[who], world["cls"], evt, ann.range_id).status_code == 404, who
    assert _audits(db_session) == []


def test_a_student_gets_no_live_view(client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    assert _live(client, world["ann"], world["cls"], evt).status_code == 403
    assert _watch(client, world["ann"], world["cls"], evt, ann.range_id).status_code == 403


def test_an_observers_console_is_marked_observed_and_audited(client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    r = _watch(client, world["observer"], world["cls"], evt, ann.range_id)
    assert r.status_code == 200, r.text
    assert r.json()["observed"] is True and r.json()["view_only"] is False
    (row,) = _audits(db_session)
    assert row["as"] == "observer" and row["_user"] == world["observer"].id and row["machine"] == "ws1"
    assert row["event_id"] == str(evt.id) and row["class_role"] == "observer"
    # An observer's console is never anyone's attendance.
    assert db_session.query(AuditLog).filter(AuditLog.action == "class_range_open").count() == 0


def test_observers_get_the_status_grid_but_no_console_by_default(client, world, db_session, monkeypatch):
    monkeypatch.delenv("CLASS_OBSERVER_CONSOLES", raising=False)
    evt, ann, _ = _individual(db_session, world)
    body = _live(client, world["observer"], world["cls"], evt).json()
    assert len(body["students"]) == 2 and body["consoles_open"] is False
    assert _watch(client, world["observer"], world["cls"], evt, ann.range_id).status_code == 403
    assert _audits(db_session) == []
    # Staff keep theirs, and say so.
    assert _live(client, world["lead"], world["cls"], evt).json()["consoles_open"] is True
    assert _watch(client, world["lead"], world["cls"], evt, ann.range_id).status_code == 200
    assert _watch(client, world["admin"], world["cls"], evt, ann.range_id, "dc").status_code == 200
    # A demoted assistant who is now a platform observer is an observer here too.
    _staff(db_session, world["cls"], world["other_observer"], "assistant")
    db_session.commit()
    assert _watch(client, world["other_observer"], world["cls"], evt, ann.range_id).status_code == 403


def test_an_observer_watches_only_the_students_machines(client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    assert _watch(client, world["observer"], world["cls"], evt, ann.range_id, "dc").status_code == 403
    assert _watch(client, world["lead"], world["cls"], evt, ann.range_id, "dc").status_code == 200


def test_a_platform_observer_on_the_staff_list_as_an_assistant_still_watches_as_an_observer(
        client, world, db_session):
    evt, ann, _ = _individual(db_session, world)
    # An assistant later made a platform observer: the staff row still says assistant.
    _staff(db_session, world["cls"], world["other_observer"], "assistant")
    db_session.commit()
    assert _live(client, world["other_observer"], world["cls"], evt).json()["role"] == "observer"
    r = _watch(client, world["other_observer"], world["cls"], evt, ann.range_id)
    assert r.status_code == 200 and r.json()["observed"] is True
    assert _watch(client, world["other_observer"], world["cls"], evt, ann.range_id, "dc").status_code == 403
    (row,) = _audits(db_session)
    assert row["as"] == "observer"


def test_an_observers_console_is_view_only_where_the_hypervisor_enforces_it(client, world, db_session):
    MockConsole.supports_view_only = True
    evt, ann, _ = _individual(db_session, world)
    assert _live(client, world["observer"], world["cls"], evt).json()["view_only_consoles"] is True
    r = _watch(client, world["observer"], world["cls"], evt, ann.range_id)
    assert r.status_code == 200 and r.json()["view_only"] is True and r.json()["observed"] is False
    # The lead's is a normal console.
    r = _watch(client, world["lead"], world["cls"], evt, ann.range_id)
    assert r.json()["view_only"] is False and r.json()["observed"] is False


@pytest.mark.parametrize("who", ["lead", "admin"])
def test_the_lead_and_admins_open_consoles_as_staff(client, world, db_session, who):
    evt, ann, _ = _individual(db_session, world)
    assert _watch(client, world[who], world["cls"], evt, ann.range_id).status_code == 200
    (row,) = _audits(db_session)
    assert row["as"] == "staff" and row["_user"] == world[who].id


def test_staff_consoles_only_during_the_window_and_only_on_the_sessions_ranges(client, world, db_session):
    db = db_session
    later = _session(db, world["cls"], world["tpl"], NOW + timedelta(days=1), state=EventState.scheduled)
    ann = _range(db, world["cls"], world["tpl"], world["ann_entry"])
    other_shared = _range(db, world["other"], world["tpl"])
    db.commit()
    assert _watch(client, world["lead"], world["cls"], later, ann.range_id).status_code == 403
    live = _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1))
    db.commit()
    assert _watch(client, world["lead"], world["cls"], live, other_shared.range_id).status_code == 404
    assert _watch(client, world["lead"], world["cls"], live, ann.range_id, "nope").status_code == 404
    assert _audits(db) == []


def test_a_collective_sessions_live_view_shows_the_shared_range_and_teams(client, world, db_session):
    db = db_session
    shared = _range(db, world["cls"], world["tpl"])
    evt = _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), mode="collective",
                   range_id=shared.range_id)
    db.commit()
    assert _console(client, world["ann"], shared.range_id).status_code == 200
    body = _live(client, world["observer"], world["cls"], evt).json()
    assert body["shared_range"]["range_id"] == str(shared.range_id)
    seen = {s["display_name"]: s["last_seen_at"] for s in body["students"]}
    assert seen["Ann"] is not None and seen["Bob"] is None
    assert _watch(client, world["observer"], world["cls"], evt, shared.range_id).status_code == 200


def test_several_vms_of_one_node_each_open_their_own_console(client, world, db_session):
    db = db_session
    shared = _range(db, world["cls"], world["tpl"])
    rng = db.get(Range, shared.range_id)
    r8 = str(rng.id)[:8]
    # A template node with count 2: both VMs carry node_id "ws1".
    rng.provisioner_output = json.dumps({"vms": [
        {"vm_id": "vm-a", "name": f"{r8}-ws1-0", "node_id": "ws1"},
        {"vm_id": "vm-b", "name": f"{r8}-ws1-1", "node_id": "ws1"},
    ]})
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), mode="collective", range_id=rng.id)
    db.commit()
    act_as(world["ann"])
    listed = client.get(f"/classes/attending/ranges/{rng.id}/consoles").json()["machines"]
    assert [m["name"] for m in listed] == [f"{r8}-ws1-0", f"{r8}-ws1-1"]
    r = client.post(f"/classes/attending/ranges/{rng.id}/consoles/{r8}-ws1-1")
    assert r.status_code == 200 and r.json()["url"].startswith("mock://console/vm-b")
    (row,) = _audits(db)
    assert row["vm_id"] == "vm-b"


def test_a_collective_session_on_an_exercises_own_range(client, world, db_session):
    """No class range row: the session books an exercise's range. The Student reaches it
    while the session is under way (as their credentials do), and it counts as attendance."""
    db = db_session
    ex = real_exercise(db, world["a"].id)
    rng = db.get(Range, ex.range_id)
    r8 = str(rng.id)[:8]
    rng.state, rng.provisioner_backend = RangeState.ready, "mock"
    rng.provisioner_output = json.dumps({"vms": [{"vm_id": "vm-x", "name": f"{r8}-ws1", "node_id": "ws1"}]})
    db.add(RangeCredential(tenant_id=rng.tenant_id, range_id=rng.id, machine="ws1", username="analyst",
                           sealed_password="x", student_visible=True, applied=True))
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), mode="collective", range_id=rng.id,
             exercise_id=ex.id)
    db.commit()
    assert _console(client, world["ann"], rng.id).status_code == 200
    assert _console(client, world["cid"], rng.id).status_code == 404  # another class's Student
    (row,) = _audits(db)
    assert row["class_id"] == str(world["cls"].id) and row["booked_range"] == "exercise"
    assert db.query(AuditLog).filter(AuditLog.action == "class_range_open").count() == 1


# ── Live Class follows the class's observer list ─────────────────────────────


def test_live_class_of_a_class_exercise_only_for_its_observers(client, world, db_session):
    db = db_session
    ex = real_exercise(db, world["a"].id)
    _session(db, world["cls"], world["tpl"], NOW - timedelta(hours=1), mode="collective", exercise_id=ex.id)
    db.commit()
    act_as(world["observer"])
    assert client.get(f"/ops/exercises/{ex.id}/class").status_code == 200
    act_as(world["other_observer"])
    assert client.get(f"/ops/exercises/{ex.id}/class").status_code == 404
    act_as(world["lead"])  # instructors keep their rule
    assert client.get(f"/ops/exercises/{ex.id}/class").status_code == 200


def test_live_class_of_an_exercise_with_no_class_keeps_the_tenant_rule(client, world, db_session):
    ex = real_exercise(db_session, world["a"].id)
    db_session.commit()
    act_as(world["other_observer"])
    assert client.get(f"/ops/exercises/{ex.id}/class").status_code == 200
    act_as(world["b_observer"])
    assert client.get(f"/ops/exercises/{ex.id}/class").status_code == 404
