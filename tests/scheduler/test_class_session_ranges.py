"""Classes 5: per-session ranges, kept all week, balanced teams (ADR 0014 §9-11, §17, §19; #282).

The rules under test:

- A class session is *individual* (a range per Student on the roll call, from the session's
  range template) or *collective* (one shared range). The course sets a tenant's default;
  the instructor overrides it per session when booking.
- An individual session's size is the per-seat size (the template's VMs) times its seats.
  Approval sizes it from the roll call then; a roll-call addition that fits grows it with no
  re-approval, and editing the session afterwards does not send it back for approval.
- A class's ranges are built at the provisioning lead before the first session that needs
  them, once, and kept across its sessions. They are torn down after the last session (a
  teardown that always runs, ADR 0013 §7), when the class is cancelled, or (one seat) when
  its Student is taken off the roll call. One seat that fails does not hold up the others,
  and is reported on the class.
- Collective sessions get balanced teams from the roll call; the lead moves people before
  the session. Exercise and telemetry rosters of class sessions follow the roll call.
- A Student sees only their own seat's credentials; instructors see every seat; observers
  see the seats' status and never credentials. Another tenant sees nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.classes import ranges as class_ranges
from app.classes import sessions as class_sessions
from app.classes.models import Class, ClassRange, ClassSession, RollCallEntry, RollCallSettings
from app.enrollment import ensure_enrollment
from app.models import AuditLog, Course, Exercise, ExerciseState, Range, RangeState, Scenario, Template, User, UserRole
from app.range_ops.models import RangeOperation
from app.scheduler import clock
from app.scheduler.capacity import Resources
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy.orm import sessionmaker

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
MON = DAY + timedelta(hours=9)
TUE = MON + timedelta(days=1)
LEAD, GRACE = timedelta(minutes=30), timedelta(minutes=15)
END = datetime.combine((DAY + timedelta(days=5)).date(), datetime.min.time(), tzinfo=UTC)  # the class's last day is over

# One Student's range: 2 VMs, 4 vCPU, 8 GB RAM, 100 GB disk.
SEAT_YAML = """
nodes:
  - id: kali
    os: kali
    specs: {cores: 2, memory_mb: 4096, disk_gb: 40}
  - id: web01
    os: ubuntu-2404
    specs: {cores: 2, memory_mb: 4096, disk_gb: 60}
accounts:
  - {machine: web01, username: analyst, generate: true, student_visible: true}
"""
SEAT = Resources(4, 8192, 100)
# The class's shared exercise range: 3 VMs, 12 vCPU.
SHARED_YAML = """
nodes:
  - id: dc01
    os: win2022
    count: 3
    specs: {cores: 4, memory_mb: 8192, disk_gb: 80}
"""


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        self.sent.append({"to": to})
        return True


@pytest.fixture(autouse=True)
def quiet_mail():
    with patch("app.notifications.get_channel", return_value=Outbox()):
        yield


@pytest.fixture
def cluster(monkeypatch):
    """100 vCPU, 400 GB RAM, 4000 GB disk, no overhead."""
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(400 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "4000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")


@pytest.fixture
def dispatched():
    sent: list[tuple[str, tuple]] = []

    def fake(name, *args):
        sent.append((name, args))
        return f"task-{len(sent)}"

    with patch("app.celery_client.dispatch", side_effect=fake):
        yield sent


def _student(db, tenant_id, email: str) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0].title(),
             role=UserRole.student, tenant_id=tenant_id, source="ad")
    db.add(u)
    db.flush()
    return u


def _as_current(u: User):
    from app.auth import CurrentUser

    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


STUDENTS = ["ann", "bob", "cat"]


@pytest.fixture
def world(db_session, client, cluster):
    db = db_session
    a, b = real_tenant(db, "rng-a"), real_tenant(db, "rng-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    seat_tpl = Template(id=uuid.uuid4(), name="Analyst seat", yaml=SEAT_YAML, tenant_id=a.id)
    shared_tpl = Template(id=uuid.uuid4(), name="Exercise range", yaml=SHARED_YAML, tenant_id=a.id)
    db.add_all([course, seat_tpl, shared_tpl])
    db.flush()
    for t in (a, b):
        db.add(RollCallSettings(tenant_id=t.id, email_domains=["corp.test"]))
    w = {
        "a": a, "b": b, "course": course, "seat_tpl": seat_tpl, "shared_tpl": shared_tpl,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        "enrolled_only": _student(db, a.id, "eve@corp.test"),
    }
    w["students"] = {n: _student(db, a.id, f"{n}@corp.test") for n in STUDENTS}
    ensure_enrollment(db, user_id=w["enrolled_only"].id, course_id=course.id, tenant_id=a.id)
    db.flush()
    act_as(w["lead"])
    r = client.post("/classes", json={
        "name": "C201-26-03", "course_id": str(course.id),
        "start_date": str(DAY.date()), "end_date": str((DAY + timedelta(days=4)).date()),
        "assistant_ids": [w["assistant"].id], "observer_ids": [w["observer"].id],
    })
    assert r.status_code == 201, r.text
    w["class"] = r.json()
    w["cid"] = r.json()["id"]
    r = client.post(f"/classes/{w['cid']}/roll-call", json={"emails": [f"{n}@corp.test" for n in STUDENTS]})
    assert r.status_code == 200 and len(r.json()["added"]) == 3, r.text
    return w


def _book(client, w, start=MON, hours=3, mode=None, template="seat_tpl", as_who=None, **extra) -> dict:
    act_as(as_who or w["lead"])
    body = {
        "name": extra.pop("name", "Lab day"), "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(), "class_id": w["cid"], **extra,
    }
    if template:
        body["template_id"] = str(w[template].id)
    if mode:
        body["range_mode"] = mode
    r = client.post("/schedule/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _approve(client, w, ev) -> dict:
    act_as(w["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def _evt(db, ev) -> ScheduledEvent:
    row = db.get(ScheduledEvent, uuid.UUID(ev["id"]))
    db.refresh(row)
    return row


def _cls(db, w) -> Class:
    return db.get(Class, uuid.UUID(w["cid"]))


def _rows(db, w) -> list[ClassRange]:
    db.expire_all()
    return db.query(ClassRange).filter(ClassRange.class_id == uuid.UUID(w["cid"])).all()


def _seat_of(db, w, name: str) -> ClassRange:
    entry = db.query(RollCallEntry).filter_by(email=f"{name}@corp.test").one()
    return next(r for r in _rows(db, w) if r.entry_id == entry.id)


def _builds(sent) -> list[str]:
    return [args[0] for name, args in sent if name == "provision_range"]


def _destroys(sent) -> list[str]:
    return [args[0] for name, args in sent if name == "destroy_range"]


def _ready(db, rows) -> None:
    """The worker reports each build done."""
    for r in rows:
        rng = db.get(Range, r.range_id)
        rng.state = RangeState.ready
    db.commit()


# ── Mode per session ───────────────────────────────────────────────────────


def test_without_a_course_default_a_session_is_collective(client, db_session, world):
    ev = _book(client, world, template="shared_tpl")
    assert ev["range_mode"] == "collective"
    link = db_session.query(ClassSession).filter_by(event_id=uuid.UUID(ev["id"])).one()
    assert link.range_mode == "collective"
    assert (ev["vcpu_total"], ev["vm_count"]) == (12, 3)  # the shared range, sized by its template


def test_the_course_default_applies_and_the_instructor_overrides_it(client, db_session, world):
    url = f"/classes/course-range-modes/{world['course'].id}"
    act_as(world["lead"])
    assert client.get(url).json() == {"course_id": str(world["course"].id), "mode": "collective", "source": "default"}
    r = client.put(url, json={"mode": "individual"})
    assert r.status_code == 200 and r.json()["source"] == "setting"
    assert _book(client, world)["range_mode"] == "individual"
    assert _book(client, world, start=TUE, mode="collective", template="shared_tpl")["range_mode"] == "collective"
    # Per tenant: another tenant's default is its own, and it cannot set ours.
    act_as(world["b_admin"])
    assert client.get(url).status_code == 404  # a's own course: not b's to see
    act_as(world["observer"])
    assert client.put(url, json={"mode": "collective"}).status_code == 403
    assert db_session.query(AuditLog).filter_by(action="course_range_mode_set").count() == 1


def test_an_individual_session_needs_a_template_and_no_fixed_range(client, db_session, world):
    act_as(world["lead"])
    base = {"name": "x", "start_time": MON.isoformat(), "end_time": (MON + timedelta(hours=2)).isoformat(),
            "class_id": world["cid"], "range_mode": "individual"}
    r = client.post("/schedule/events", json={**base, "vcpu_total": 10})
    assert r.status_code == 422 and "template" in r.json()["detail"]
    rng = Range(name="fixed", template_id=world["seat_tpl"].id, tenant_id=world["a"].id, state=RangeState.created)
    db_session.add(rng)
    db_session.flush()
    r = client.post("/schedule/events", json={**base, "template_id": str(world["seat_tpl"].id), "range_id": str(rng.id)})
    assert r.status_code == 422 and "range" in r.json()["detail"]
    sc = Scenario(id=uuid.uuid4(), name="Ransomware", yaml="id: s\n", tenant_id=world["a"].id)
    db_session.add(sc)
    db_session.flush()
    r = client.post("/schedule/events", json={**base, "template_id": str(world["seat_tpl"].id), "scenario_id": str(sc.id)})
    assert r.status_code == 422 and "scenario" in r.json()["detail"]  # no per-Student exercise
    # The mode belongs to class sessions.
    r = client.post("/schedule/events", json={"name": "plain", "start_time": (MON + timedelta(days=30)).isoformat(),
                                              "end_time": (MON + timedelta(days=30, hours=1)).isoformat(),
                                              "range_mode": "individual", "vcpu_total": 1})
    assert r.status_code == 422


# ── Seat demand ────────────────────────────────────────────────────────────


def test_seat_demand_is_the_templates_size_for_individual_sessions_only(client, db_session, world):
    ind = _book(client, world, mode="individual")
    col = _book(client, world, start=TUE, mode="collective", template="shared_tpl")
    cls = _cls(db_session, world)
    assert class_sessions.seat_demand(db_session, cls, _evt(db_session, ind)) == SEAT
    assert class_sessions.seat_demand(db_session, cls, _evt(db_session, col)) == Resources()
    # Booked with three Students on the roll call: three seats.
    assert (ind["vcpu_total"], ind["ram_mb_total"], ind["disk_gb_total"], ind["vm_count"]) == (12, 3 * 8192, 300, 6)


def test_approval_sizes_an_individual_session_from_the_roll_call_then(client, db_session, world):
    ev = _book(client, world, mode="individual")
    act_as(world["lead"])  # two more before anyone approves: nothing is held yet
    client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test", "fay@corp.test"]})
    out = _approve(client, world, ev)
    assert (out["vcpu_total"], out["vm_count"]) == (20, 10)


def test_approval_checks_capacity_with_the_real_demand(client, db_session, world):
    ev = _book(client, world, mode="individual")  # 12 vCPU
    db_session.add(ScheduledEvent(name="big", state=EventState.scheduled, tenant_id=world["b"].id, start_time=MON,
                                  end_time=MON + timedelta(hours=3), vcpu_total=90))
    db_session.flush()
    act_as(world["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 409 and any("vCPU: need 12" in x for x in r.json()["detail"]["reasons"])


def test_an_addition_that_fits_is_automatic_and_does_not_re_pend_the_session(client, db_session, world):
    ev = _approve(client, world, _book(client, world, mode="individual"))
    act_as(world["lead"])
    r = client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test"]})
    assert [e["email"] for e in r.json()["added"]] == ["dan@corp.test"]
    evt = _evt(db_session, ev)
    assert evt.state == EventState.scheduled and (evt.vcpu_total, evt.vm_count) == (16, 8)
    # The lead then renames it: the same size, so it stays approved (#297 review note).
    r = client.put(f"/schedule/events/{ev['id']}", json={
        "name": "Lab day (renamed)", "start_time": ev["start_time"], "end_time": ev["end_time"],
        "template_id": str(world["seat_tpl"].id), "class_id": world["cid"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "scheduled" and r.json()["vcpu_total"] == 16
    assert not db_session.query(AuditLog).filter_by(resource_id=ev["id"], action="approval_requested").filter(
        AuditLog.detail.like("scheduled ->%")).all()
    # Changing the mode is a change of size: it needs approving again (ADR 0014 §16).
    r = client.put(f"/schedule/events/{ev['id']}", json={
        "name": "Lab day", "start_time": ev["start_time"], "end_time": ev["end_time"], "range_mode": "collective",
        "template_id": str(world["shared_tpl"].id), "class_id": world["cid"],
    })
    assert r.status_code == 200 and r.json()["state"] == "pending_approval"
    assert r.json()["range_mode"] == "collective" and r.json()["vcpu_total"] == 12


def test_an_addition_that_does_not_fit_waits_for_an_approver(client, db_session, world):
    ev = _approve(client, world, _book(client, world, mode="individual"))  # 12 vCPU
    db_session.add(ScheduledEvent(name="big", state=EventState.scheduled, tenant_id=world["b"].id, start_time=MON,
                                  end_time=MON + timedelta(hours=3), vcpu_total=86))  # 2 vCPU left
    db_session.flush()
    act_as(world["lead"])
    r = client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test"]})
    assert r.json()["added"] == [] and [p["email"] for p in r.json()["pending_approval"]] == ["dan@corp.test"]
    assert _evt(db_session, ev).vcpu_total == 12 and _evt(db_session, ev).state == EventState.scheduled


def test_a_removal_frees_the_seat(client, db_session, world):
    ev = _approve(client, world, _book(client, world, mode="individual"))
    bob = db_session.query(RollCallEntry).filter_by(email="bob@corp.test").one()
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['cid']}/roll-call/{bob.id}").status_code == 204
    assert (_evt(db_session, ev).vcpu_total, _evt(db_session, ev).vm_count) == (8, 4)


# ── Built once at the lead, kept all week ──────────────────────────────────


def test_individual_ranges_are_built_at_the_lead_once_per_seat(client, db_session, world, dispatched):
    ev = _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD - timedelta(minutes=1))
    assert _rows(db_session, world) == []  # not yet
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    assert len(rows) == 3 and {r.kind for r in rows} == {"seat"}
    assert sorted(_builds(dispatched)) == sorted(str(r.range_id) for r in rows)
    for r in rows:
        rng = db_session.get(Range, r.range_id)
        assert rng.tenant_id == world["a"].id and rng.template_id == world["seat_tpl"].id
        assert rng.state == RangeState.provisioning and r.builds == 1
        ops = db_session.query(RangeOperation).filter_by(range_id=r.range_id).all()
        assert [o.action for o in ops] == ["provision"]  # through range operations, as any build
    clock.tick(db_session, now=MON - LEAD + timedelta(minutes=1))
    clock.tick(db_session, now=MON + timedelta(minutes=5))
    assert len(_builds(dispatched)) == 3  # once
    assert _evt(db_session, ev).range_id is None  # a booking of many ranges names none
    assert _evt(db_session, ev).state == EventState.active


def test_ranges_are_kept_across_sessions_and_torn_down_when_the_class_ends(client, db_session, world, dispatched):
    mon = _approve(client, world, _book(client, world, mode="individual", name="Mon"))
    tue = _approve(client, world, _book(client, world, start=TUE, mode="individual", name="Tue"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    _ready(db_session, rows)
    clock.tick(db_session, now=MON)
    clock.tick(db_session, now=MON + timedelta(hours=3) + GRACE)  # Monday is over
    assert _evt(db_session, mon).state == EventState.completed
    assert _destroys(dispatched) == []  # kept for Tuesday
    clock.tick(db_session, now=TUE - LEAD)
    assert len(_builds(dispatched)) == 3  # Tuesday reuses them
    clock.tick(db_session, now=TUE)
    clock.tick(db_session, now=TUE + timedelta(hours=3) + GRACE)
    assert _evt(db_session, tue).state == EventState.completed
    assert _destroys(dispatched) == []  # kept until the class's last day is over (ADR 0014 §10)
    clock.tick(db_session, now=END + GRACE)
    assert sorted(_destroys(dispatched)) == sorted(str(r.range_id) for r in rows)
    for r in _rows(db_session, world):
        assert r.torn_down_at is not None and r.teardown_reason == "class_ended"
        (op,) = db_session.query(RangeOperation).filter_by(range_id=r.range_id, action="destroy").all()
        assert op.window_bypass == "booking_ended"  # lease expiry: a maintenance window never blocks it
    clock.tick(db_session, now=END + timedelta(hours=1))
    assert len(_destroys(dispatched)) == 3  # once


def test_a_cancelled_class_tears_its_ranges_down(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    _approve(client, world, _book(client, world, start=TUE, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['cid']}").status_code == 204
    clock.tick(db_session, now=MON + timedelta(minutes=10))
    assert len(_destroys(dispatched)) == 3
    assert {r.teardown_reason for r in _rows(db_session, world)} == {"class_cancelled"}


def test_a_class_past_its_end_date_tears_its_ranges_down(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    cls = _cls(db_session, world)
    cls.end_date = MON.date()
    db_session.commit()
    # Still mid-session but the class's last day is over (an overrun): torn down regardless.
    clock.tick(db_session, now=MON + timedelta(days=1, hours=1))
    assert len(_destroys(dispatched)) == 3


def test_a_removed_students_range_is_torn_down_and_the_others_kept(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    bob_seat = _seat_of(db_session, world, "bob")
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['cid']}/roll-call/{bob_seat.entry_id}").status_code == 204
    clock.tick(db_session, now=MON + timedelta(minutes=10))
    assert _destroys(dispatched) == [str(bob_seat.range_id)]
    (op,) = db_session.query(RangeOperation).filter_by(range_id=bob_seat.range_id, action="destroy").all()
    assert op.window_bypass is None  # a normal destroy: a maintenance window defers it
    assert _seat_of(db_session, world, "bob").teardown_reason == "student_removed"
    # A Student added mid-session gets their seat built on the next tick.
    act_as(world["lead"])
    client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test"]})
    clock.tick(db_session, now=MON + timedelta(minutes=11))
    assert _seat_of(db_session, world, "dan").range_id is not None
    assert len(_builds(dispatched)) == 4


def test_collective_sessions_share_one_range_kept_across_sessions(client, db_session, world, dispatched):
    mon = _approve(client, world, _book(client, world, mode="collective", template="shared_tpl", name="Ex 1"))
    tue = _approve(client, world, _book(client, world, start=TUE, mode="collective", template="shared_tpl", name="Ex 2"))
    clock.tick(db_session, now=MON - LEAD)
    (row,) = _rows(db_session, world)
    assert row.kind == "shared" and row.entry_id is None
    assert _evt(db_session, mon).range_id == row.range_id
    assert _builds(dispatched) == [str(row.range_id)]
    _ready(db_session, [row])
    clock.tick(db_session, now=MON + timedelta(hours=3) + GRACE)
    assert _destroys(dispatched) == []
    clock.tick(db_session, now=TUE - LEAD)
    assert _evt(db_session, tue).range_id == row.range_id and len(_builds(dispatched)) == 1
    clock.tick(db_session, now=TUE)
    clock.tick(db_session, now=TUE + timedelta(hours=3) + GRACE)
    assert _destroys(dispatched) == []
    clock.tick(db_session, now=END + GRACE)
    assert _destroys(dispatched) == [str(row.range_id)]


def test_one_failed_seat_does_not_block_the_others_and_is_reported(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    real = class_ranges.range_lifecycle.provision_for_booking
    calls = {"n": 0}

    def flaky(db, range_id, before_build=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("vCenter said no")
        return real(db, range_id, before_build)

    # The clock rolls a failed seat back. On the test's own joined transaction a plain rollback
    # would end the test's, so it runs on a session that rolls back to a savepoint instead.
    clock_db = sessionmaker(bind=db_session.connection(), autoflush=False, join_transaction_mode="create_savepoint")()
    with patch.object(class_ranges.range_lifecycle, "provision_for_booking", side_effect=flaky):
        clock.tick(clock_db, now=MON - LEAD)
    rows = _rows(db_session, world)
    assert len(rows) == 3 and len(_builds(dispatched)) == 2
    broken = [r for r in rows if r.last_error]
    assert len(broken) == 1 and "vCenter said no" in broken[0].last_error
    # The worker fails one more build; the class reports both.
    built = [r for r in rows if not r.last_error]
    db_session.get(Range, built[0].range_id).state = RangeState.failed
    db_session.get(Range, built[0].range_id).error_message = "clone failed"
    db_session.get(Range, built[1].range_id).state = RangeState.ready
    db_session.commit()
    act_as(world["lead"])
    out = client.get(f"/classes/{world['cid']}/ranges").json()
    assert out["failed"] == 2
    status = {r["email"]: r["status"] for r in out["ranges"]}
    assert sorted(status.values()) == ["failed", "failed", "ready"]
    # The lead retries a failed seat.
    rid = next(r["id"] for r in out["ranges"] if r["range_id"] == str(built[0].range_id))
    assert next(r for r in out["ranges"] if r["id"] == rid)["error"] == "clone failed"
    ready = next(r["id"] for r in out["ranges"] if r["status"] == "ready")
    assert client.post(f"/classes/{world['cid']}/ranges/{ready}/retry").status_code == 409
    act_as(world["assistant"])
    assert client.post(f"/classes/{world['cid']}/ranges/{rid}/retry").status_code == 403
    act_as(world["lead"])
    r = client.post(f"/classes/{world['cid']}/ranges/{rid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "building"
    # The next tick builds the seat whose dispatch blew up, without re-sending the others.
    before = len(_builds(dispatched))
    clock.tick(db_session, now=MON - LEAD + timedelta(minutes=1))
    assert len(_builds(dispatched)) == before + 1


def test_a_seat_that_breaks_before_its_build_holds_up_none_of_the_others(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    real = class_ranges.range_lifecycle.create_for_booking
    calls = {"n": 0}

    def flaky(db, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return real(db, **kw)

    clock_db = sessionmaker(bind=db_session.connection(), autoflush=False, join_transaction_mode="create_savepoint")()
    with patch.object(class_ranges.range_lifecycle, "create_for_booking", side_effect=flaky):
        clock.tick(clock_db, now=MON - LEAD)
    assert len(_builds(dispatched)) == 2
    clock.tick(clock_db, now=MON - LEAD + timedelta(minutes=1))  # the next tick builds the third
    assert len(_builds(dispatched)) == 3 and len({r.range_id for r in _rows(db_session, world)}) == 3


def test_fifteen_seats_fan_out_in_one_tick_each_its_own_operation(client, db_session, world, dispatched):
    act_as(world["lead"])
    client.post(f"/classes/{world['cid']}/roll-call", json={"emails": [f"s{i}@corp.test" for i in range(12)]})
    ev = _approve(client, world, _book(client, world, mode="individual"))
    assert ev["vcpu_total"] == 15 * 4
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    assert len(rows) == 15 and len(set(_builds(dispatched))) == 15
    assert db_session.query(RangeOperation).filter(RangeOperation.range_id.in_([r.range_id for r in rows])).count() == 15


def test_capacity_counts_a_class_range_once(client, db_session, world, dispatched):
    """Seat ranges kept up between sessions are the class's next session's demand, not extra."""
    from app.scheduler.capacity import get_capacity_provider

    _approve(client, world, _book(client, world, mode="individual"))
    tue = _book(client, world, start=TUE, mode="individual")
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    # Tuesday fits with 12 vCPU for itself, even though 12 vCPU of seat ranges run now.
    db_session.add(ScheduledEvent(name="big", state=EventState.scheduled, tenant_id=world["b"].id, start_time=TUE,
                                  end_time=TUE + timedelta(hours=3), vcpu_total=80))
    db_session.flush()
    _approve(client, world, tue)
    provider = get_capacity_provider(db_session)
    held = provider.committed(db_session, TUE, TUE + timedelta(hours=1))
    assert held.resources.vcpu == 80 + 12


# ── Teams ──────────────────────────────────────────────────────────────────


@pytest.fixture
def seven(world, client, db_session):
    """Seven Students on the roll call with accounts, and one invited who has none yet."""
    extra = ["dan", "fay", "gus", "hal"]
    world["students"].update({n: _student(db_session, world["a"].id, f"{n}@corp.test") for n in extra})
    _student(db_session, world["a"].id, "ivy@corp.test")  # in the tenant, not on the roll call
    act_as(world["lead"])
    r = client.post(f"/classes/{world['cid']}/roll-call",
                    json={"emails": [f"{n}@corp.test" for n in extra] + ["newbie@corp.test"]})
    assert len(r.json()["added"]) == 5
    return world


def test_collective_sessions_split_the_roll_call_into_balanced_teams(client, db_session, seven):
    w = seven
    ev = _book(client, w, mode="collective", template="shared_tpl")
    url = f"/classes/{w['cid']}/sessions/{ev['id']}/teams"
    act_as(w["lead"])
    r = client.post(url, json={"team_count": 3})
    assert r.status_code == 200, r.text
    sizes = sorted(len(t["members"]) for t in r.json()["teams"])
    assert sizes == [2, 2, 3]
    assert [u["email"] for u in r.json()["unassigned"]] == ["newbie@corp.test"]  # no account yet
    members = [m["user_id"] for t in r.json()["teams"] for m in t["members"]]
    assert len(set(members)) == 7
    # Moving one Student from the biggest team to a smallest.
    big = max(r.json()["teams"], key=lambda t: len(t["members"]))
    small = min(r.json()["teams"], key=lambda t: len(t["members"]))
    who = big["members"][0]["user_id"]
    r = client.put(f"{url}/members/{who}", json={"team_id": small["id"]})
    assert r.status_code == 200
    placed = {m["user_id"]: t["id"] for t in r.json()["teams"] for m in t["members"]}
    assert placed[who] == small["id"] and len(placed) == 7  # one team each
    # Splitting again rebalances everyone.
    r = client.post(url, json={"team_count": 2})
    assert sorted(len(t["members"]) for t in r.json()["teams"]) == [3, 4]
    # Assistants and observers read them; only the lead or an admin changes them.
    for who_ in ("assistant", "observer"):
        act_as(w[who_])
        assert client.get(url).status_code == 200
        assert client.post(url, json={"team_count": 2}).status_code == 403
    act_as(w["b_admin"])
    assert client.get(url).status_code == 404


def test_teams_refuse_individual_sessions_strangers_and_a_started_session(client, db_session, world):
    w = world
    ind = _book(client, w, mode="individual")
    act_as(w["lead"])
    assert client.post(f"/classes/{w['cid']}/sessions/{ind['id']}/teams", json={"team_count": 2}).status_code == 409
    col = _book(client, w, start=TUE, mode="collective", template="shared_tpl")
    url = f"/classes/{w['cid']}/sessions/{col['id']}/teams"
    r = client.post(url, json={"team_count": 2})
    team = r.json()["teams"][0]["id"]
    stranger = w["enrolled_only"]  # enrolled on the course, not on the roll call
    assert client.put(f"{url}/members/{stranger.id}", json={"team_id": team}).status_code == 422
    evt = _evt(db_session, col)
    evt.start_time = datetime.now(UTC) - timedelta(minutes=5)
    evt.end_time = datetime.now(UTC) + timedelta(hours=2)
    db_session.commit()
    ann = w["students"]["ann"]
    assert client.put(f"{url}/members/{ann.id}", json={"team_id": team}).status_code == 409


def test_a_removed_student_leaves_the_sessions_teams(client, db_session, world):
    w = world
    col = _book(client, w, mode="collective", template="shared_tpl")
    url = f"/classes/{w['cid']}/sessions/{col['id']}/teams"
    act_as(w["lead"])
    client.post(url, json={"team_count": 2})
    bob = db_session.query(RollCallEntry).filter_by(email="bob@corp.test").one()
    client.delete(f"/classes/{w['cid']}/roll-call/{bob.id}")
    members = [m["user_id"] for t in client.get(url).json()["teams"] for m in t["members"]]
    assert str(w["students"]["bob"].id) not in members and len(members) == 2


def test_teams_get_the_sessions_exercise_when_it_is_created(client, db_session, world, dispatched):
    w = world
    sc = Scenario(id=uuid.uuid4(), name="Ransomware", yaml="id: s\n", tenant_id=w["a"].id)
    db_session.add(sc)
    db_session.flush()
    col = _approve(client, w, _book(client, w, mode="collective", template="shared_tpl", scenario_id=str(sc.id)))
    act_as(w["lead"])
    teams = client.post(f"/classes/{w['cid']}/sessions/{col['id']}/teams", json={"team_count": 2}).json()["teams"]
    clock.tick(db_session, now=MON - LEAD)
    evt = _evt(db_session, col)
    assert evt.exercise_id is not None
    from app.models import Team

    assert {db_session.get(Team, uuid.UUID(t["id"])).exercise_id for t in teams} == {evt.exercise_id}


# ── Rosters follow the roll call ───────────────────────────────────────────


def _running_exercise(db, w, ev) -> Exercise:
    evt = _evt(db, ev)
    ex = db.get(Exercise, evt.exercise_id)
    ex.state = ExerciseState.running
    ex.started_at = datetime.now(UTC) - timedelta(minutes=5)
    evt.start_time = datetime.now(UTC) - timedelta(minutes=10)
    evt.end_time = datetime.now(UTC) + timedelta(hours=2)
    db.commit()
    return ex


def test_exercise_and_telemetry_rosters_follow_the_roll_call(client, db_session, world, dispatched):
    from app import aar_access, exercise_participants, telemetry_access

    w = world
    sc = Scenario(id=uuid.uuid4(), name="Ransomware", yaml="id: s\n", tenant_id=w["a"].id)
    db_session.add(sc)
    db_session.flush()
    col = _approve(client, w, _book(client, w, mode="collective", template="shared_tpl", scenario_id=str(sc.id)))
    clock.tick(db_session, now=MON - LEAD)
    ex = _running_exercise(db_session, w, col)
    rng = db_session.get(Range, ex.range_id)
    ann, eve = _as_current(w["students"]["ann"]), _as_current(w["enrolled_only"])
    assert exercise_participants.may_submit(db_session, ann, ex) is True
    assert exercise_participants.may_submit(db_session, eve, ex) is False  # enrolled, not on the roll call
    assert telemetry_access._takes_part(db_session, ann, rng) is True
    assert telemetry_access._takes_part(db_session, eve, rng) is False
    assert w["students"]["ann"].id in aar_access.participants(db_session, ex)
    assert w["enrolled_only"].id not in aar_access.participants(db_session, ex)
    # Taken off the roll call: off the roster.
    bob = db_session.query(RollCallEntry).filter_by(email="bob@corp.test").one()
    act_as(w["lead"])
    client.delete(f"/classes/{w['cid']}/roll-call/{bob.id}")
    assert exercise_participants.may_submit(db_session, _as_current(w["students"]["bob"]), ex) is False


# ── Credentials ────────────────────────────────────────────────────────────


def _applied(db, rows) -> None:
    from app.range_credentials.models import RangeCredential

    for r in rows:
        for c in db.query(RangeCredential).filter_by(range_id=r.range_id).all():
            c.applied = True
    db.commit()


def test_each_student_sees_only_their_own_seats_credentials(client, db_session, world, dispatched):
    w = world
    _approve(client, w, _book(client, w, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, w)
    _ready(db_session, rows)
    _applied(db_session, rows)
    ann_seat, bob_seat = _seat_of(db_session, w, "ann"), _seat_of(db_session, w, "bob")

    def creds(who, seat):
        act_as(who)
        return client.get(f"/ranges/{seat.range_id}/credentials")

    ann = _as_current(w["students"]["ann"])
    r = creds(ann, ann_seat)
    assert r.status_code == 200 and [a["username"] for a in r.json()["accounts"]] == ["analyst"]
    cred = r.json()["accounts"][0]["id"]
    r = client.post(f"/ranges/{ann_seat.range_id}/credentials/{cred}/reveal")
    assert r.status_code == 200 and r.json()["password"]
    audit = db_session.query(AuditLog).filter_by(action="range_credential_reveal").order_by(AuditLog.timestamp).all()[-1]
    assert str(ann_seat.id) in audit.detail  # through which class seat
    assert creds(ann, bob_seat).status_code == 404  # another Student's seat
    assert creds(_as_current(w["enrolled_only"]), ann_seat).status_code == 404
    # The class's instructors see every seat, staff accounts included.
    for who in ("lead", "assistant"):
        r = creds(w[who], bob_seat)
        assert r.status_code == 200 and r.json()["scope"] == "all"
    # Observers see status, never credentials (ADR 0014 §19).
    assert creds(w["observer"], ann_seat).status_code == 403
    act_as(w["observer"])
    out = client.get(f"/classes/{w['cid']}/ranges").json()
    assert {r["status"] for r in out["ranges"]} == {"ready"}
    assert "password" not in str(out)
    # Another tenant: nothing.
    assert creds(w["b_admin"], ann_seat).status_code == 404
    # A removed Student loses their seat's credentials at once.
    act_as(w["lead"])
    client.delete(f"/classes/{w['cid']}/roll-call/{ann_seat.entry_id}")
    assert creds(ann, ann_seat).status_code == 404


def test_a_student_sees_only_their_own_seat_on_the_class_page(client, db_session, world, dispatched):
    w = world
    _approve(client, w, _book(client, w, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    act_as(_as_current(w["students"]["ann"]))
    out = client.get(f"/classes/{w['cid']}/ranges")
    assert out.status_code == 200
    (mine,) = out.json()["ranges"]
    assert mine["entry_id"] == str(_seat_of(db_session, w, "ann").entry_id)
    assert "bob@corp.test" not in out.text and "cat@corp.test" not in out.text


# ── Tenant isolation ───────────────────────────────────────────────────────


def test_another_tenant_reaches_none_of_a_classs_ranges(client, db_session, world, dispatched):
    w = world
    _approve(client, w, _book(client, w, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    row = _rows(db_session, w)[0]
    for who in ("b_admin", "b_instructor"):
        act_as(w[who])
        assert client.get(f"/classes/{w['cid']}/ranges").status_code == 404
        assert client.post(f"/classes/{w['cid']}/ranges/{row.id}/retry").status_code == 404
    for r in _rows(db_session, w):
        assert r.tenant_id == w["a"].id and db_session.get(Range, r.range_id).tenant_id == w["a"].id


def test_a_class_range_cannot_be_booked_by_another_session(client, db_session, world, dispatched):
    w = world
    _approve(client, w, _book(client, w, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    row = _rows(db_session, w)[0]
    act_as(w["admin"])
    r = client.post("/schedule/events", json={
        "name": "steal", "start_time": (MON + timedelta(days=20)).isoformat(),
        "end_time": (MON + timedelta(days=20, hours=1)).isoformat(), "range_id": str(row.range_id)})
    assert r.status_code == 409 and "class" in r.json()["detail"]


def test_the_teams_endpoints_treat_a_class_sessions_team_as_the_classs(client, db_session, world):
    """/teams (routers/admin.py, class_setup.scope): its lead before the session, roll call only."""
    w = world
    col = _book(client, w, start=TUE, mode="collective", template="shared_tpl")
    act_as(w["lead"])
    team = client.post(f"/classes/{w['cid']}/sessions/{col['id']}/teams", json={"team_count": 2}).json()["teams"][0]
    member = team["members"][0]["user_id"]
    assert client.delete(f"/teams/{team['id']}/members/{member}").status_code == 204
    assert client.post(f"/teams/{team['id']}/members", params={"user_id": member}).status_code == 201
    stranger = w["enrolled_only"].id
    assert client.post(f"/teams/{team['id']}/members", params={"user_id": str(stranger)}).status_code == 422
    act_as(w["assistant"])  # an instructor, but not the class's lead
    assert client.delete(f"/teams/{team['id']}/members/{member}").status_code == 403
    act_as(w["b_instructor"])
    assert client.delete(f"/teams/{team['id']}/members/{member}").status_code == 404


def test_an_aar_roster_counts_the_roll_call_at_the_runs_end(client, db_session, world):
    """Taken off after the run: still a participant of that run. Taken off before: not."""
    w = world
    ev = _book(client, w, mode="collective", template="shared_tpl")
    eid = uuid.UUID(ev["id"])
    run_end = datetime.now(UTC) + timedelta(hours=1)
    ann = db_session.query(RollCallEntry).filter_by(email="ann@corp.test").one()
    bob = db_session.query(RollCallEntry).filter_by(email="bob@corp.test").one()
    ann.status, ann.removed_at = "removed", run_end + timedelta(hours=1)
    bob.status, bob.removed_at = "removed", run_end - timedelta(minutes=30)
    db_session.commit()
    at_end = class_sessions.roll_call_user_ids(db_session, [eid], as_of=run_end)
    assert w["students"]["ann"].id in at_end and w["students"]["bob"].id not in at_end
    now = class_sessions.roll_call_user_ids(db_session, [eid])
    assert now == {w["students"]["cat"].id}


# ── Adversarial review of #299 ─────────────────────────────────────────────

def _plain(client, w, start, hours, vcpu, as_who=None):
    act_as(as_who or w["admin"])
    return client.post("/schedule/events", json={
        "name": "someone else", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(), "vcpu_total": vcpu})


def _destroy_ops(db, rows):
    return db.query(RangeOperation).filter(
        RangeOperation.range_id.in_([r.range_id for r in rows]), RangeOperation.action == "destroy").all()


def test_r1_a_classs_ranges_hold_capacity_between_its_sessions(client, db_session, world):
    """Approved days ahead, before anything is built: the evening between two sessions is
    the class's (its ranges stay up), so a booking that needs the whole cluster then is refused."""
    _approve(client, world, _book(client, world, mode="individual", name="Mon"))
    _approve(client, world, _book(client, world, start=TUE, mode="individual", name="Tue"))
    r = _plain(client, world, MON + timedelta(hours=9), 2, 95)  # Monday 18:00-20:00
    assert r.status_code == 409 and "vCPU" in r.json()["detail"]
    assert _plain(client, world, MON + timedelta(hours=9), 2, 88).status_code == 201  # 12 are the class's
    # Until the class's last day is over, not just its last booked session.
    assert _plain(client, world, DAY + timedelta(days=3, hours=20), 1, 95).status_code == 409
    assert _plain(client, world, END + timedelta(hours=2), 1, 95).status_code == 201


def test_r1_overlapping_sessions_and_built_ranges_are_counted_once(client, db_session, world, dispatched):
    from app.scheduler.capacity import get_capacity_provider

    _approve(client, world, _book(client, world, mode="individual", name="Mon"))
    _approve(client, world, _book(client, world, start=MON + timedelta(hours=1), mode="individual", name="Mon 2",
                                  instructor_id=world["assistant"].id))
    held = get_capacity_provider(db_session).committed(db_session, MON + timedelta(hours=1), MON + timedelta(hours=2))
    assert held.resources.vcpu == 12  # one class, one template: one hold, not 12 + 12
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    evening = get_capacity_provider(db_session).committed(db_session, MON + timedelta(hours=9),
                                                          MON + timedelta(hours=10))
    assert evening.resources.vcpu == 12 and evening.ranges == 0  # the running seats are the hold, not extra


def test_r1_approving_a_later_session_checks_up_to_the_class_end(client, db_session, world):
    """A session added before the first one moves the class's hold earlier: that span is checked."""
    _approve(client, world, _book(client, world, start=TUE, mode="individual", name="Tue"))
    _plain(client, world, MON + timedelta(hours=9), 2, 90)  # Monday evening: fits now
    early = _book(client, world, mode="individual", name="Mon")
    act_as(world["admin"])
    r = client.post(f"/schedule/events/{early['id']}/approve")
    assert r.status_code == 409  # its ranges would stay up through Monday evening


def test_r2_a_removed_students_seat_waits_for_a_maintenance_window(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    _ready(db_session, _rows(db_session, world))
    bob = _seat_of(db_session, world, "bob")
    act_as(world["lead"])
    client.delete(f"/classes/{world['cid']}/roll-call/{bob.entry_id}")
    clock.tick(db_session, now=MON + timedelta(minutes=10))
    (op,) = _destroy_ops(db_session, [bob])
    assert op.window_bypass is None  # a normal destroy: no lease expired
    assert _seat_of(db_session, world, "bob").teardown_reason == "student_removed"


def test_r2_a_cancelled_class_or_session_is_a_normal_destroy(client, db_session, world, dispatched):
    ev = _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    _ready(db_session, rows)
    act_as(world["lead"])
    assert client.post(f"/schedule/events/{ev['id']}/cancel").status_code == 200  # its only session
    clock.tick(db_session, now=MON - LEAD + timedelta(minutes=1))
    ops = _destroy_ops(db_session, rows)
    assert len(ops) == 3 and {o.window_bypass for o in ops} == {None}
    assert {r.teardown_reason for r in _rows(db_session, world)} == {"no_session_needs_it"}


def test_r4_ranges_are_kept_until_the_class_ends_for_day_by_day_booking(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual", name="Mon"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    _ready(db_session, rows)
    clock.tick(db_session, now=MON + timedelta(hours=3) + GRACE)  # Monday over, nothing else booked yet
    assert _destroys(dispatched) == []
    _approve(client, world, _book(client, world, start=TUE, mode="individual", name="Tue"))
    clock.tick(db_session, now=TUE - LEAD)
    assert len(_builds(dispatched)) == 3  # Tuesday finds Monday's ranges
    clock.tick(db_session, now=TUE + timedelta(hours=3) + GRACE)
    assert _destroys(dispatched) == []  # the class still runs till its last day
    clock.tick(db_session, now=END + GRACE)
    ops = _destroy_ops(db_session, rows)
    assert len(ops) == 3 and {o.window_bypass for o in ops} == {"booking_ended"}  # the class's lease expired
    assert {r.teardown_reason for r in _rows(db_session, world)} == {"class_ended"}


def test_r3a_an_exercise_cannot_carry_a_class_range_onto_another_booking(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="collective", template="shared_tpl"))
    clock.tick(db_session, now=MON - LEAD)
    (row,) = _rows(db_session, world)
    ex = Exercise(id=uuid.uuid4(), name="on the class range", range_id=row.range_id, tenant_id=world["a"].id)
    db_session.add(ex)
    db_session.flush()
    later = MON + timedelta(days=20)
    r = _plain(client, world, later, 1, 1)
    assert r.status_code == 201
    act_as(world["admin"])
    r = client.put(f"/schedule/events/{r.json()['id']}", json={
        "name": "steal", "start_time": later.isoformat(), "end_time": (later + timedelta(hours=1)).isoformat(),
        "exercise_id": str(ex.id), "vcpu_total": 1})
    assert r.status_code == 409 and "class" in r.json()["detail"]


def test_r3b_a_session_on_another_classs_range_is_not_treated_as_this_classs(client, db_session, world, dispatched):
    w = world
    _approve(client, w, _book(client, w, mode="collective", template="shared_tpl"))
    clock.tick(db_session, now=MON - LEAD)
    (x_row,) = _rows(db_session, w)
    act_as(w["lead"])
    other = client.post("/classes", json={"name": "C202", "course_id": str(w["course"].id),
                                          "start_date": str(DAY.date()), "end_date": str(DAY.date())}).json()
    ev = _approve(client, w, _book(client, {**w, "cid": other["id"]}, start=MON + timedelta(hours=4),
                                   mode="collective", template="shared_tpl"))
    db_session.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(ev["id"])).update(
        {ScheduledEvent.range_id: x_row.range_id}, synchronize_session=False)
    db_session.commit()
    clock.tick(db_session, now=MON + timedelta(hours=4) - LEAD)
    assert db_session.query(ClassRange).filter(ClassRange.class_id == uuid.UUID(other["id"])).count() == 0


def test_r3c_an_edit_keeps_the_range_its_exercise_brings(client, db_session, world, dispatched):
    w = world
    ev = _approve(client, w, _book(client, w, mode="collective", template="shared_tpl"))
    clock.tick(db_session, now=MON - LEAD)
    rng = Range(id=uuid.uuid4(), name="own", template_id=w["shared_tpl"].id, tenant_id=w["a"].id,
                state=RangeState.created)
    db_session.add(rng)
    db_session.flush()
    ex = Exercise(id=uuid.uuid4(), name="elsewhere", range_id=rng.id, tenant_id=w["a"].id)
    db_session.add(ex)
    # Not built yet: back to scheduled, so it can be edited.
    evt = _evt(db_session, ev)
    evt.state = EventState.scheduled
    db_session.commit()
    act_as(w["lead"])
    r = client.put(f"/schedule/events/{ev['id']}", json={
        "name": "x", "start_time": ev["start_time"], "end_time": ev["end_time"], "class_id": w["cid"],
        "template_id": str(w["shared_tpl"].id), "exercise_id": str(ex.id)})
    assert r.status_code == 200, r.text
    assert r.json()["range_id"] == str(rng.id)


def test_r5_seat_changes_recompute_the_size_from_the_roll_call(client, db_session, world):
    ev = _approve(client, world, _book(client, world, mode="individual"))
    db_session.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(ev["id"])).update(
        {ScheduledEvent.vcpu_total: 999}, synchronize_session=False)  # drifted
    db_session.commit()
    act_as(world["lead"])
    client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test"]})
    assert (_evt(db_session, ev).vcpu_total, _evt(db_session, ev).vm_count) == (16, 8)
    dan = db_session.query(RollCallEntry).filter_by(email="dan@corp.test").one()
    client.delete(f"/classes/{world['cid']}/roll-call/{dan.id}")
    assert _evt(db_session, ev).vcpu_total == 12


def test_r5_an_addition_takes_the_booking_lock_while_an_individual_session_waits(client, db_session, world):
    _book(client, world, mode="individual")  # pending: it will hold a seat per Student when approved
    calls = []
    with patch.object(class_sessions.scheduler, "serialize_bookings", side_effect=lambda db: calls.append(1)):
        act_as(world["lead"])
        client.post(f"/classes/{world['cid']}/roll-call", json={"emails": ["dan@corp.test"]})
    assert calls


def test_r6_teardown_does_not_mark_a_rebuilt_seat_torn_down(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    _ready(db_session, rows)
    stale = rows[0]
    newer = Range(id=uuid.uuid4(), name="rebuilt", template_id=world["seat_tpl"].id, tenant_id=world["a"].id,
                  state=RangeState.ready)
    db_session.add(newer)
    db_session.flush()
    db_session.query(ClassRange).filter(ClassRange.id == stale.id).update(
        {ClassRange.range_id: newer.id}, synchronize_session=False)  # another replica rebuilt it meanwhile
    cls = _cls(db_session, world)
    cls.status = "cancelled"
    db_session.flush()
    class_ranges._teardown_if_done(db_session, stale, MON)  # still holds the old range id
    db_session.expire_all()
    assert db_session.get(ClassRange, stale.id).torn_down_at is None


def _class_exercise(client, db_session, w, dispatched, instructor=None) -> tuple[dict, Exercise]:
    sc = Scenario(id=uuid.uuid4(), name="Ransomware", yaml="id: s\n", tenant_id=w["a"].id)
    db_session.add(sc)
    db_session.flush()
    extra = {"instructor_id": instructor.id} if instructor else {}
    ev = _approve(client, w, _book(client, w, start=datetime.now(UTC) + timedelta(minutes=20), mode="collective",
                                   template="shared_tpl", scenario_id=str(sc.id), **extra))
    clock.tick(db_session)
    return ev, db_session.get(Exercise, _evt(db_session, ev).exercise_id)


def test_r7_teams_endpoints_cannot_get_around_the_class(client, db_session, world, dispatched):
    from app.classes.models import ClassSessionTeam
    from app.models import Team

    w = world
    ev, ex = _class_exercise(client, db_session, w, dispatched, instructor=w["assistant"])
    # The session's instructor (an assistant) is not the class's lead: no team of their own.
    act_as(w["assistant"])
    assert client.post("/teams", json={"name": "rogue", "exercise_id": str(ex.id)}).status_code == 403
    t = Team(name="T", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(t)
    db_session.flush()
    link = db_session.query(ClassSession).filter_by(event_id=uuid.UUID(ev["id"])).one()
    db_session.add(ClassSessionTeam(tenant_id=w["a"].id, session_id=link.id, team_id=t.id))
    db_session.commit()
    # Even an admin (user:update) puts only Students on the roll call on a class session's team.
    act_as(w["admin"])
    r = client.post(f"/teams/{t.id}/members", params={"user_id": str(w["enrolled_only"].id)})
    assert r.status_code == 422
    # Deleting a class session's team removes its link first.
    assert client.delete(f"/teams/{t.id}").status_code == 204


def test_r9_retry_needs_an_open_class_and_a_seat_still_on_the_roll_call(client, db_session, world, dispatched):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    for r in rows:
        db_session.get(Range, r.range_id).state = RangeState.failed
    db_session.commit()
    bob = _seat_of(db_session, world, "bob")
    act_as(world["lead"])
    client.delete(f"/classes/{world['cid']}/roll-call/{bob.entry_id}")
    assert client.post(f"/classes/{world['cid']}/ranges/{bob.id}/retry").status_code == 409
    ann = _seat_of(db_session, world, "ann")
    act_as(world["admin"])
    client.delete(f"/classes/{world['cid']}")
    assert client.post(f"/classes/{world['cid']}/ranges/{ann.id}/retry").status_code == 409


def test_r9_credentials_close_with_the_class_and_never_reach_its_observers(client, db_session, world, dispatched):
    w = world
    instructor_observer = real_user(db_session, UserRole.instructor, w["a"].id)
    from app.classes.models import ClassStaff

    db_session.add(ClassStaff(class_id=uuid.UUID(w["cid"]), user_id=uuid.UUID(instructor_observer.id), role="observer"))
    db_session.commit()
    _approve(client, w, _book(client, w, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, w)
    _ready(db_session, rows)
    _applied(db_session, rows)
    ann_seat = _seat_of(db_session, w, "ann")
    act_as(instructor_observer)  # an instructor by role, but this class's observer (ADR 0014 §19)
    assert client.get(f"/ranges/{ann_seat.range_id}/credentials").status_code == 403
    act_as(_as_current(w["students"]["ann"]))
    assert client.get(f"/ranges/{ann_seat.range_id}/credentials").status_code == 200
    _cls(db_session, w).status = "cancelled"
    db_session.commit()
    assert client.get(f"/ranges/{ann_seat.range_id}/credentials").status_code == 404


def test_r9_the_teardown_pass_pages_past_its_page_size(client, db_session, world, dispatched, monkeypatch):
    _approve(client, world, _book(client, world, mode="individual"))
    clock.tick(db_session, now=MON - LEAD)
    rows = _rows(db_session, world)
    _ready(db_session, rows)
    monkeypatch.setattr(class_ranges, "PAGE", 2)
    act_as(world["lead"])
    client.delete(f"/classes/{world['cid']}")
    clock.tick(db_session, now=MON)
    assert len(_destroy_ops(db_session, rows)) == 3
