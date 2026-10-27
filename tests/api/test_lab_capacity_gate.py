"""Self-serve labs use only the capacity left after approved classes (ADR 0014 §18, #288).

* Start lab is refused with "Labs are busy, next free at HH:MM" (the site's time) when
  approved bookings and approved classes, over their whole span, leave no room; a booking
  awaiting approval holds nothing and never refuses a lab.
* The next free time follows what holds the capacity: a class to its end, a running lab to
  when it would close if unused.
* The message names no class, course or tenant: capacity is site-wide, its holders are not.
* An idle lab closes after the idle limit (4 hours by default), not before, with one
  warning to the Student first; a class's range is never closed by this rule.

The concurrent last-slot start is in test_lab_capacity_gate_postgres.py.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from _release_kit import CATALOGUE, CROSSWALK, LAB_PROFILE, build
from app.classes import holds
from app.classes.models import Class, ClassRange, ClassSession, RollCallEntry
from app.lab_sessions import capacity_gate, service
from app.lab_sessions.models import LabSession
from app.models import Course, GoldenImage, Range, RangeState, Template, Tenant, User, UserRole
from app.notifications.models import UserNotification
from app.scheduler.capacity import TEARDOWN_GRACE, Resources
from app.scheduler.models import EventState, ScheduledEvent
from test_course_releases import OTHER_TENANT, upload
from test_lab_sessions import act, lab, student, until, worker  # noqa: F401 — fixtures

OTHER = uuid.UUID(OTHER_TENANT)
SECRET_CLASS = "Operation NIGHTJAR C201-26-03"
SECRET_COURSE = "Nightjar Advanced Intrusion"


@pytest.fixture(autouse=True)
def cluster(monkeypatch):
    """Ten vCPU in all, no headroom kept back (env supply: no hosts are discovered here)."""
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "10")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", "1048576")
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "100000")
    monkeypatch.setenv("SITE_TIMEZONE", "UTC")


@pytest.fixture
def long_lab(client, db_session, tmp_path, worker, monkeypatch):  # noqa: F811
    """A lab whose profile keeps it a day when idle and 12 hours at most, so the platform's
    idle limit is what closes it."""
    monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    db_session.add(
        GoldenImage(catalogue_id="ubuntu-lts", hypervisor="vsphere", template_name="ubuntu-2404", enabled=True)
    )
    db_session.commit()
    profile = {**LAB_PROFILE, "lifetime": {"idle_minutes": 1440, "max_minutes": 720}}
    rid = upload(client, build(tmp_path, range_ordinals=frozenset({6}), lab_profile=profile)).json()["id"]
    assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
    return uuid.UUID(rid)


def _other_tenant(db) -> User:
    if db.get(Tenant, OTHER) is None:
        db.add(Tenant(id=OTHER, name="Other Org", slug="other"))
        db.flush()
    lead = User(email=f"lead{uuid.uuid4().hex[:6]}@other.test", display_name="Lead", role=UserRole.instructor,
                tenant_id=OTHER, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(lead)
    db.flush()
    return lead


def _booking(db, *, state=EventState.scheduled, start=None, hours=3, vcpu=9) -> ScheduledEvent:
    """Another tenant's class booking holding ``vcpu`` of the ten."""
    _other_tenant(db)
    start = start or datetime.now(UTC) + timedelta(hours=1)
    evt = ScheduledEvent(name=SECRET_CLASS, tenant_id=OTHER, state=state, start_time=start,
                         end_time=start + timedelta(hours=hours), vcpu_total=vcpu, ram_mb_total=1024, disk_gb_total=10)
    db.add(evt)
    db.commit()
    return evt


def _class(db, *, state=EventState.scheduled, seats=2, end_in_days=3) -> Class:
    """Another tenant's class: one session in two hours, individual ranges of 5 vCPU per
    seat, the class running for ``end_in_days`` days. Approved, it holds 5 x seats vCPU from
    the session's provisioning lead to the class's last day."""
    lead = _other_tenant(db)
    course = Course(name=SECRET_COURSE, tenant_id=OTHER)
    db.add(course)
    tpl = Template(name="nightjar-seat", tenant_id=OTHER, yaml=yaml.safe_dump(
        {"nodes": [{"id": "ws", "os": "ubuntu-lts", "specs": {"cores": 5, "memory_mb": 1024, "disk_gb": 10}}]}))
    db.add(tpl)
    db.flush()
    today = datetime.now(UTC).date()
    cls = Class(tenant_id=OTHER, name=SECRET_CLASS, course_id=course.id, start_date=today,
                end_date=today + timedelta(days=end_in_days), lead_instructor_id=lead.id)
    db.add(cls)
    db.flush()
    start = datetime.now(UTC) + timedelta(hours=2)
    evt = ScheduledEvent(name=SECRET_CLASS, tenant_id=OTHER, state=state, start_time=start,
                         end_time=start + timedelta(hours=3), template_id=tpl.id, course_id=course.id,
                         vcpu_total=5 * seats, ram_mb_total=1024 * seats, disk_gb_total=10 * seats)
    db.add(evt)
    db.flush()
    db.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=OTHER, range_mode="individual"))
    for i in range(seats):
        db.add(RollCallEntry(class_id=cls.id, tenant_id=OTHER, email=f"s{i}@other.test", status="signed_in"))
    db.commit()
    return cls


def _try_launch(db, user, release_id):
    """As the API does: a refusal undoes the whole launch; tasks go out after the commit."""
    try:
        with db.begin_nested():
            session, created = service.launch(
                db, tenant_id=user.tenant_id, user_id=user.id, release_id=release_id, activity_id="mod_006"
            )
    except service.LabRefusedError:
        db.info.pop(service.OUTBOX, None)
        raise
    db.commit()
    service.flush_outbox(db)
    return session, created


def _ceil15(t: datetime) -> datetime:
    return capacity_gate._ceil(t)


# ── refusal and admission ────────────────────────────────────────────


class TestGate:
    def test_a_lab_is_refused_while_an_approved_class_holds_the_capacity(self, lab, db_session, worker):  # noqa: F811
        _, rid, _ = lab
        _class(db_session, state=EventState.scheduled)
        with pytest.raises(service.LabsBusyError) as refused:
            _try_launch(db_session, student(db_session), rid)
        assert re.match(r"^Labs are busy, next free at \d\d:\d\d", refused.value.message(db_session))
        assert db_session.query(LabSession).count() == 0
        assert not [c for c in worker.calls if c[0] == "provision_range"]

    def test_an_approved_class_holds_its_whole_span_not_only_its_sessions(self, lab, db_session):  # noqa: F811
        _, rid, _ = lab
        cls = _class(db_session, state=EventState.scheduled, end_in_days=3)
        need = Resources(2, 2048, 20)
        at = capacity_gate.next_free(db_session, need, timedelta(hours=4), datetime.now(UTC))
        # The session ends in five hours; the class's ranges are held to its last day.
        assert at is not None and at >= holds.last_day_end(cls.end_date) + TEARDOWN_GRACE - timedelta(minutes=15)

    def test_a_booking_awaiting_approval_holds_nothing(self, lab, db_session):  # noqa: F811
        _, rid, _ = lab
        _class(db_session, state=EventState.pending_approval)
        _booking(db_session, state=EventState.pending_approval, vcpu=10)
        session, created = _try_launch(db_session, student(db_session), rid)
        assert created and session.state == "provisioning" and not session.error

    def test_an_approved_booking_holds_its_window(self, lab, db_session):  # noqa: F811
        _, rid, _ = lab
        _booking(db_session, start=datetime.now(UTC) + timedelta(hours=1), vcpu=9)
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, student(db_session), rid)

    def test_room_left_beside_an_approved_booking_is_used(self, lab, db_session):  # noqa: F811
        _, rid, _ = lab
        _booking(db_session, vcpu=8)  # 2 of 10 left: exactly one lab
        session, _ = _try_launch(db_session, student(db_session), rid)
        assert session.state == "provisioning"
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, student(db_session), rid)

    def test_a_queued_lab_keeps_waiting_with_the_message(self, lab, db_session, monkeypatch):  # noqa: F811
        """A lab already in the queue (behind a quota) is not refused; it waits, and its page
        says when labs are next free."""
        client, rid, _ = lab
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
        session, _ = _try_launch(db_session, student(db_session), rid)
        assert session.state == "queued"
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")
        _booking(db_session, vcpu=10)
        act(db_session, service.advance, session)
        assert session.state == "queued" and session.error == capacity_gate.BUSY
        shown = client.get(f"/lab-sessions/{session.id}").json()
        assert shown["state"] == "queued" and shown["error"].startswith("Labs are busy, next free at")


# ── the next free time ───────────────────────────────────────────────


class TestNextFree:
    def test_after_a_booking_its_teardown_grace_then_the_next_mark(self, lab, db_session):  # noqa: F811
        _, rid, _ = lab
        evt = _booking(db_session, start=datetime.now(UTC) + timedelta(minutes=30), hours=2, vcpu=9)
        at = capacity_gate.next_free(db_session, Resources(2, 2048, 20), timedelta(hours=4), datetime.now(UTC))
        assert at == _ceil15(evt.end_time.replace(tzinfo=UTC) + TEARDOWN_GRACE)

    def test_a_running_lab_frees_its_room_when_it_would_close_unused(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, _ = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "3")  # one 2-vCPU lab at a time
        first, _ = _try_launch(db_session, student(db_session), rid)
        until(db_session, first, "ready")
        closes = datetime.now(UTC) + timedelta(minutes=50)
        first.idle_expires_at = closes
        db_session.commit()
        with pytest.raises(service.LabsBusyError) as refused:
            _try_launch(db_session, student(db_session), rid)
        message = refused.value.message(db_session)
        assert message == f"Labs are busy, next free at {_ceil15(closes):%H:%M}" or (
            # just before midnight UTC the next free time is tomorrow
            message.startswith(f"Labs are busy, next free at {_ceil15(closes):%H:%M} on ")
        )

    def test_the_time_is_the_sites_local_time(self, monkeypatch):
        monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
        now = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)  # 10:00 in Toronto
        at = datetime(2026, 10, 27, 18, 30, tzinfo=UTC)
        assert capacity_gate.busy_message(at, now) == "Labs are busy, next free at 14:30"

    def test_another_day_says_which(self, monkeypatch):
        monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
        now = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
        at = datetime(2026, 10, 29, 13, 15, tzinfo=UTC)
        assert capacity_gate.busy_message(at, now) == "Labs are busy, next free at 09:15 on Thu 29 Oct"

    def test_nothing_free_in_the_horizon_says_so(self):
        msg = capacity_gate.busy_message(None, datetime(2026, 10, 27, tzinfo=UTC))
        assert msg.startswith("Labs are busy, and none is free in the next")

    def test_an_unknown_site_time_zone_falls_back_to_utc(self, monkeypatch):
        monkeypatch.setenv("SITE_TIMEZONE", "Mars/Olympus")
        now = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
        assert capacity_gate.busy_message(now + timedelta(hours=1), now) == "Labs are busy, next free at 15:00"


# ── tenant isolation ─────────────────────────────────────────────────


class TestNoLeak:
    def test_the_refusal_names_no_class_course_or_tenant(self, lab, db_session):  # noqa: F811
        client, _, course_id = lab
        _class(db_session, state=EventState.scheduled)
        r = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail.startswith("Labs are busy, next free at")
        for secret in (SECRET_CLASS, "NIGHTJAR", SECRET_COURSE, "Other Org", OTHER_TENANT, "vCPU"):
            assert secret not in r.text

    def test_the_moodle_start_lab_page_shows_the_message_and_nothing_else(self, lab, db_session):  # noqa: F811
        from app.routers import integrations

        _, _, course_id = lab
        _class(db_session, state=EventState.scheduled)
        s = student(db_session)
        db_session.commit()
        resp = None
        try:
            integrations._launch_lab(db_session, s, f"{course_id}:mod_006")
        except service.LabsBusyError as exc:
            resp = integrations._labs_busy_page(exc.message(db_session))
        assert resp is not None and resp.status_code == 409
        body = resp.body.decode()
        assert "The lab machines are all in use" in body
        assert "Labs are busy, next free at" in body
        for secret in (SECRET_CLASS, "NIGHTJAR", SECRET_COURSE, "Other Org"):
            assert secret not in body


# ── idle teardown ────────────────────────────────────────────────────


def _at(monkeypatch, t: datetime) -> None:
    monkeypatch.setattr(service, "_now", lambda: t)


class TestIdle:
    def _active(self, db, release_id, monkeypatch):
        s = student(db)
        session, _ = _try_launch(db, s, release_id)
        until(db, session, "ready")
        t0 = datetime.now(UTC)
        _at(monkeypatch, t0)
        act(db, service.touch, session)
        assert session.state == "active"
        return session, s, t0

    def test_an_idle_lab_closes_at_four_hours_and_not_before(self, long_lab, db_session, monkeypatch):
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(hours=4) - timedelta(minutes=1))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state == "active"
        _at(monkeypatch, t0 + timedelta(hours=4))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state in ("expired", "cleaning", "destroyed") and session.end_reason == "idle"

    def test_activity_keeps_it_open(self, long_lab, db_session, monkeypatch):
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(hours=3))
        act(db_session, service.touch, session)  # the lab page's heartbeat
        _at(monkeypatch, t0 + timedelta(hours=6))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state == "active"

    def test_the_idle_limit_is_a_setting(self, long_lab, db_session, monkeypatch):
        monkeypatch.setenv("LAB_IDLE_LIMIT_MINUTES", "30")
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(minutes=31))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.end_reason == "idle"

    def test_a_shorter_idle_time_in_the_profile_still_applies(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, _ = lab  # the kit's profile closes after 60 idle minutes
        session, _, t0 = self._active(db_session, rid, monkeypatch)
        _at(monkeypatch, t0 + timedelta(minutes=61))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.end_reason == "idle"

    def test_the_student_is_warned_once_before_it_closes(self, long_lab, db_session, monkeypatch):
        session, s, t0 = self._active(db_session, long_lab, monkeypatch)

        def warnings():
            return db_session.query(UserNotification).filter(UserNotification.user_id == s.id).all()

        _at(monkeypatch, t0 + timedelta(hours=3, minutes=40))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert warnings() == []
        _at(monkeypatch, t0 + timedelta(hours=3, minutes=46))
        service.sweep(db_session)  # the sweep asks the consoles first
        _at(monkeypatch, t0 + timedelta(hours=3, minutes=50))
        service.sweep(db_session)  # the sweep asks the consoles first
        (note,) = warnings()
        assert note.title == "Your lab closes soon" and note.tenant_id == s.tenant_id
        assert session.state == "active"
        act(db_session, service.touch, session)  # back again: a later idle spell warns again
        assert session.idle_warned_at is None

    def test_reaching_the_maximum_lifetime_is_recorded_as_expiry(self, long_lab, db_session, monkeypatch):
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        for h in range(1, 13):
            _at(monkeypatch, t0 + timedelta(hours=h))
            act(db_session, service.touch, session)
            service.sweep(db_session)  # the sweep asks the consoles first
        assert session.end_reason == "expired"


class TestClassRangesAreNeverClosed:
    def _class_range(self, db, range_id) -> None:
        cls = _class(db, state=EventState.scheduled)
        tpl = db.query(Template).filter(Template.name == "nightjar-seat").one()
        db.add(ClassRange(tenant_id=OTHER, class_id=cls.id, template_id=tpl.id, kind="seat",
                          seat_key=str(uuid.uuid4()), range_id=range_id))
        db.commit()

    def test_a_class_range_is_left_alone_by_the_lab_sweep(self, long_lab, db_session, worker, monkeypatch):  # noqa: F811
        tpl = Template(name="class-seat", yaml="nodes: [{id: a, os: ubuntu-lts}]", tenant_id=OTHER)
        _other_tenant(db_session)
        db_session.add(tpl)
        db_session.flush()
        rng = Range(name="nightjar-seat-1", template_id=tpl.id, tenant_id=OTHER, state=RangeState.ready)
        db_session.add(rng)
        db_session.flush()
        self._class_range(db_session, rng.id)
        _at(monkeypatch, datetime.now(UTC) + timedelta(days=2))
        service.sweep(db_session)
        assert db_session.get(Range, rng.id).state == RangeState.ready
        assert not [c for c in worker.calls if c[0] == "destroy_range"]

    def test_a_lab_that_points_at_a_class_range_is_not_torn_down(self, long_lab, db_session, worker, monkeypatch):  # noqa: F811
        s = student(db_session)
        session, _ = _try_launch(db_session, s, long_lab)
        until(db_session, session, "ready")
        self._class_range(db_session, session.range_id)  # however it came to be
        _at(monkeypatch, datetime.now(UTC) + timedelta(hours=5))
        act(db_session, service.advance, session)
        assert session.state == "failed" and session.end_reason == "class_range"
        assert db_session.get(Range, session.range_id).state == RangeState.ready
        assert not [c for c in worker.calls if c[0] == "destroy_range"]


# ── review of e7516c50 ───────────────────────────────────────────────


class TestResetHoldsItsRoom:
    """B1: a rebuild reset destroyed the lab's range (its room looked free) and rebuilt it
    without the gate: another Student's launch took the room in between, then the rebuild
    over-committed the site."""

    def test_a_student_launching_during_anothers_reset_is_refused(
        self, lab, db_session, worker, monkeypatch  # noqa: F811
    ):
        from app.capacity import service as cap

        _, rid, _ = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "3")  # one 2-vCPU lab fits, not two
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01,pg-lab-02")
        profile = service._profile
        monkeypatch.setattr(service, "_profile", lambda db, s: {**profile(db, s), "reset": {"mode": "rebuild"}})
        worker.hold = True
        a, _ = _try_launch(db_session, student(db_session), rid)
        for _ in range(8):
            worker.run()
            act(db_session, service.advance, a)
            if a.state == "ready":
                break
        assert a.state == "ready", a.state
        act(db_session, service.reset, a)  # its range goes down; the rebuild follows
        worker.run()  # the old range is destroyed: the gap
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, student(db_session), rid)
        for _ in range(4):
            act(db_session, service.advance, a)
            worker.run()
        assert sum(r.vcpu for r in cap.running_ranges(db_session)) <= 3


def _lab_class(db, course_id, *, seats=4, per=2, tenant=None, end=None) -> tuple[ScheduledEvent, Class]:
    """A class of the lab's course whose approved session is running now, sized for ``seats``
    labs of ``per`` vCPU, with no range or template of its own; its roll call is empty."""
    now = datetime.now(UTC)
    tenant = tenant or uuid.UUID(int=1)
    evt = ScheduledEvent(name="C304 serial 3", tenant_id=tenant, state=EventState.active,
                         start_time=now - timedelta(minutes=5), end_time=end or now + timedelta(hours=3),
                         course_id=course_id, vcpu_total=seats * per, ram_mb_total=seats * 2048,
                         disk_gb_total=seats * 20)
    db.add(evt)
    lead = db.query(User).filter(User.tenant_id == tenant).first()
    today = now.date()
    cls = Class(tenant_id=tenant, name="C304-26-03", course_id=course_id, start_date=today, end_date=today,
                lead_instructor_id=lead.id)
    db.add(cls)
    db.flush()
    db.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=tenant, range_mode="collective"))
    db.commit()
    return evt, cls


def _on_roll_call(db, cls: Class) -> User:
    s = student(db, tenant=cls.tenant_id)
    db.add(RollCallEntry(class_id=cls.id, tenant_id=cls.tenant_id, email=s.email, user_id=s.id, status="signed_in"))
    db.commit()
    return s


def _enrolled(db, course_id) -> User:
    """Enrolled in the course (as a Moodle launch does), on no roll call."""
    from app.enrollment import ensure_enrollment

    s = student(db)
    ensure_enrollment(db, user_id=s.id, course_id=course_id, tenant_id=s.tenant_id)
    db.commit()
    return s


class TestAClassesOwnLabs:
    """B2: a class's Students were refused by their own session's hold. Their labs draw on
    it (ADR 0014 §18) while they are on its roll call; course enrolment never draws."""

    def test_all_four_students_get_in_and_a_fifth_outside_the_roll_call_is_refused(
        self, lab, db_session, monkeypatch  # noqa: F811
    ):
        _, rid, course_id = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "9")  # the session's 8, and 1 to spare: no lab
        evt, cls = _lab_class(db_session, course_id)
        for _ in range(4):
            session, _ = _try_launch(db_session, _on_roll_call(db_session, cls), rid)
            assert session.state == "provisioning" and session.booking_id == evt.id
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, _enrolled(db_session, course_id), rid)  # enrolled, not on the roll call
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, _on_roll_call(db_session, cls), rid)  # beyond the session's size

    def test_the_sessions_labs_are_counted_once(self, lab, db_session):  # noqa: F811
        from app.scheduler.capacity import ClusterCapacity

        _, rid, course_id = lab
        _, cls = _lab_class(db_session, course_id)
        for _ in range(4):
            _try_launch(db_session, _on_roll_call(db_session, cls), rid)
        now = datetime.now(UTC)
        (c,) = ClusterCapacity(db_session).committed_many(db_session, [(now, now + timedelta(minutes=15))])
        assert c.resources.vcpu == 8  # the session's hold; its four labs inside it

    def test_room_left_beside_a_session_still_serves_self_serve_labs(self, lab, db_session):  # noqa: F811
        _, rid, course_id = lab
        _lab_class(db_session, course_id)  # 8 of 10
        session, _ = _try_launch(db_session, student(db_session), rid)
        assert session.state == "provisioning" and session.booking_id is None

    def test_a_booking_with_no_class_is_never_drawn_on(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, course_id = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "9")
        now = datetime.now(UTC)
        db_session.add(ScheduledEvent(name="plain", tenant_id=uuid.UUID(int=1), state=EventState.active,
                                      start_time=now - timedelta(minutes=5), end_time=now + timedelta(hours=3),
                                      course_id=course_id, vcpu_total=8, ram_mb_total=8192, disk_gb_total=80))
        db_session.commit()
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, _enrolled(db_session, course_id), rid)

    def test_another_tenants_class_is_never_drawn_on(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, course_id = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "9")
        _other_tenant(db_session)
        _lab_class(db_session, course_id, tenant=OTHER)
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, _enrolled(db_session, course_id), rid)

    def test_a_moodle_launch_does_not_draw_on_another_cohorts_session(self, lab, db_session, monkeypatch):  # noqa: F811
        """Round 2, T2: Moodle's Start lab enrols the Student; that must not seat them in a
        class. The class's real Student still gets in."""
        _, rid, course_id = lab
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "3")
        evt, cls = _lab_class(db_session, course_id, seats=1)  # serial 3: one seat
        stranger = student(db_session)  # never put in any class
        with pytest.raises(service.LabsBusyError), db_session.begin_nested():
            service.launch_for_course(db_session, tenant_id=stranger.tenant_id, user_id=stranger.id,
                                      course_id=course_id, activity_id="mod_006", auto_enroll=True)
        mine, _ = _try_launch(db_session, _on_roll_call(db_session, cls), rid)
        assert mine.booking_id == evt.id and mine.state == "provisioning"

    def test_a_class_lab_ends_with_its_session(self, lab, db_session):  # noqa: F811
        """Round 2, T1: drawn labs outlived the session, ungated, over an approved booking
        after it. Now they end with the session; the next booking is not over-committed."""
        from app.scheduler.capacity import ClusterCapacity

        client, rid, course_id = lab
        now = datetime.now(UTC)
        evt, cls = _lab_class(db_session, course_id, end=now + timedelta(minutes=10))
        _other_tenant(db_session)
        db_session.add(ScheduledEvent(name="b", tenant_id=OTHER, state=EventState.scheduled,
                                      start_time=now + timedelta(minutes=45), end_time=now + timedelta(hours=4),
                                      vcpu_total=8, ram_mb_total=8192, disk_gb_total=80))
        db_session.commit()
        sessions = [_try_launch(db_session, _on_roll_call(db_session, cls), rid)[0] for _ in range(4)]
        end = evt.end_time.replace(tzinfo=UTC) + TEARDOWN_GRACE
        assert all(s.booking_id == evt.id and s.max_expires_at.replace(tzinfo=UTC) <= end for s in sessions)
        t = now + timedelta(hours=1)
        (c,) = ClusterCapacity(db_session).committed_many(db_session, [(t, t + timedelta(minutes=15))])
        assert c.resources.vcpu <= 10
        shown = client.get(f"/lab-sessions/{sessions[0].id}").json()
        assert shown["ends_with_class"] is True

    def test_a_shrunk_session_still_counts_the_labs_drawing_on_it(self, lab, db_session):  # noqa: F811
        """Round 2, T3: the session shrunk to 2 vCPU under 8 vCPU of its labs hid 6 of them."""
        _, rid, course_id = lab
        evt, cls = _lab_class(db_session, course_id, end=datetime.now(UTC) + timedelta(hours=6))
        for _ in range(4):
            _try_launch(db_session, _on_roll_call(db_session, cls), rid)
        evt.vcpu_total = 2
        db_session.commit()
        admitted = 0
        for _ in range(4):
            try:
                _try_launch(db_session, student(db_session), rid)
                admitted += 1
            except service.LabsBusyError:
                break
        running = db_session.query(LabSession).filter(LabSession.state == "provisioning").count()
        assert running * 2 <= 10 and admitted == 1

    def test_taken_off_the_roll_call_the_lab_stops_drawing(self, lab, db_session):  # noqa: F811
        from app.scheduler.capacity import ClusterCapacity

        _, rid, course_id = lab
        _, cls = _lab_class(db_session, course_id, seats=1)
        s = _on_roll_call(db_session, cls)
        _try_launch(db_session, s, rid)
        db_session.query(RollCallEntry).filter(RollCallEntry.user_id == s.id).update({"status": "removed"})
        db_session.commit()
        now = datetime.now(UTC)
        (c,) = ClusterCapacity(db_session).committed_many(db_session, [(now, now + timedelta(minutes=15))])
        assert c.resources.vcpu == 4  # the session's 2, and the lab's 2 as a self-serve lab


class TestTheForecastIsNeverUnderTheLock:
    """N2: the next free time is computed after the refused launch rolled back, and a queued
    lab's page reads it at most every 30 seconds."""

    def test_a_refused_launch_computes_no_forecast(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, _ = lab
        _booking(db_session, vcpu=10)

        def under_the_lock(*a, **k):
            raise AssertionError("next_free ran inside the launch")

        monkeypatch.setattr(capacity_gate, "next_free", under_the_lock)
        with pytest.raises(service.LabsBusyError):
            _try_launch(db_session, student(db_session), rid)

    def test_a_queued_labs_page_polls_reuse_the_forecast(self, lab, db_session, monkeypatch):  # noqa: F811
        client, rid, _ = lab
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
        session, _ = _try_launch(db_session, student(db_session), rid)
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")
        _booking(db_session, vcpu=10)
        calls = []
        real = capacity_gate.next_free
        monkeypatch.setattr(capacity_gate, "next_free", lambda *a, **k: calls.append(1) or real(*a, **k))
        decided = []
        fits = capacity_gate.fits
        monkeypatch.setattr(capacity_gate, "fits", lambda *a, **k: decided.append(1) or fits(*a, **k))
        for _ in range(5):
            assert client.get(f"/lab-sessions/{session.id}").json()["error"].startswith("Labs are busy, next")
        assert len(calls) == 1 and len(decided) == 1


class TestIdleMeansNoStudentActivity:
    """A forgotten open tab must not keep a lab open: only a beat that reports use does."""

    def test_only_a_heartbeat_reporting_use_keeps_the_lab_open(self, lab, db_session):  # noqa: F811
        client, rid, _ = lab
        session, _ = _try_launch(db_session, student(db_session), rid)
        until(db_session, session, "ready")
        before = session.idle_expires_at
        for body in (None, {"active": False}):
            r = client.post(f"/lab-sessions/{session.id}/heartbeat", json=body)
            assert r.status_code == 200, r.text
            db_session.refresh(session)
            assert session.idle_expires_at == before and session.state == "ready"
        r = client.post(f"/lab-sessions/{session.id}/heartbeat", json={"active": True})
        assert r.status_code == 200, r.text
        db_session.refresh(session)
        assert session.state == "active" and session.idle_expires_at > before


class TestApprovalCountsLabsOnlyUntilTheyEnd:
    def test_a_running_lab_does_not_hold_next_weeks_capacity(self, lab, db_session):  # noqa: F811
        from app.scheduler.capacity import ClusterCapacity

        _, rid, _ = lab
        session, _ = _try_launch(db_session, student(db_session), rid)
        until(db_session, session, "ready")
        now = datetime.now(UTC)
        week = now + timedelta(days=7)
        today, later = ClusterCapacity(db_session).committed_many(
            db_session, [(now, now + timedelta(hours=1)), (week, week + timedelta(hours=1))]
        )
        assert today.resources.vcpu == 2 and later.resources.vcpu == 0


class TestTheClassRangeBackstop:
    def test_it_ends_the_lab_once_and_the_student_can_launch_again(
        self, long_lab, db_session, worker, monkeypatch  # noqa: F811
    ):
        from app.classes import ranges as class_ranges

        s = student(db_session)
        session, _ = _try_launch(db_session, s, long_lab)
        until(db_session, session, "ready")
        TestClassRangesAreNeverClosed()._class_range(db_session, session.range_id)
        checks = []
        real = class_ranges.is_class_range
        monkeypatch.setattr(class_ranges, "is_class_range", lambda db, r: checks.append(r) or real(db, r))
        _at(monkeypatch, datetime.now(UTC) + timedelta(hours=5))
        for _ in range(3):
            service.sweep(db_session)
        assert len(checks) == 1 and session.state == "failed" and session.end_reason == "class_range"
        assert not [c for c in worker.calls if c[0] == "destroy_range"]
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "40")  # room beside the test's class
        again, created = _try_launch(db_session, s, long_lab)
        assert created and again.attempt == 2


class TestConsoleUseIsActivity:
    """Round 2: WebMKS input never reaches the lab page. Before an idle close the sweep asks
    the console backend for open console connections."""

    def _idle(self, db, release_id, monkeypatch):
        s = student(db)
        session, _ = _try_launch(db, s, release_id)
        until(db, session, "ready")
        t0 = datetime.now(UTC)
        _at(monkeypatch, t0)
        act(db, service.touch, session)
        _at(monkeypatch, t0 + timedelta(hours=4, minutes=1))
        return session, t0

    def test_an_open_console_keeps_the_lab(self, long_lab, db_session, monkeypatch):
        from app.console_backends.mock import MockConsole

        monkeypatch.setattr(MockConsole, "connections", 1)
        session, t0 = self._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state == "active"
        assert session.idle_expires_at.replace(tzinfo=UTC) >= t0 + timedelta(hours=8)

    def test_no_console_closes_it(self, long_lab, db_session, monkeypatch):
        from app.console_backends.mock import MockConsole

        monkeypatch.setattr(MockConsole, "connections", 0)
        session, _ = self._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.end_reason == "idle"

    def test_not_knowing_keeps_it(self, long_lab, db_session, monkeypatch):
        from app.console_backends.mock import MockConsole

        monkeypatch.setattr(MockConsole, "connections", None)
        session, _ = self._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state == "active" and session.end_reason == ""

    def test_a_backend_that_fails_keeps_it(self, long_lab, db_session, monkeypatch):
        from app.console_backends.mock import MockConsole

        def broken(self, db, targets):
            raise RuntimeError("vCenter is down")

        monkeypatch.setattr(MockConsole, "console_reader", broken)
        session, _ = self._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.state == "active"

    def test_the_maximum_lifetime_still_ends_it(self, long_lab, db_session, monkeypatch):
        from app.console_backends.mock import MockConsole

        monkeypatch.setattr(MockConsole, "connections", 1)
        session, t0 = self._idle(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(hours=13))
        service.sweep(db_session)  # the sweep asks the consoles first
        assert session.end_reason == "expired"


# ── round 3 (a0771453) ───────────────────────────────────────────────


@pytest.fixture
def mock_console(monkeypatch):
    from app.console_backends.mock import MockConsole

    monkeypatch.setattr(MockConsole, "connections", 0)
    monkeypatch.setattr(MockConsole, "delay", 0.0)
    monkeypatch.setattr(MockConsole, "logins", 0)
    return MockConsole


class TestTheConsoleCheckNeverStallsTheSweep:
    """B1: the console check ran a vCenter login, with no timeout, inside each lab's locked
    advance. Now the sweep asks before any lock, in one batch, under a deadline."""

    def test_a_slow_backend_does_not_delay_the_other_labs(
        self, long_lab, db_session, worker, mock_console, monkeypatch  # noqa: F811
    ):
        import time

        monkeypatch.setenv("LAB_CONSOLE_BATCH_SECONDS", "0.3")
        mock_console.delay = 3.0  # a vCenter that does not answer
        idle, _ = TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        worker.hold = True
        other, _ = _try_launch(db_session, student(db_session), long_lab)
        worker.run()  # built: the sweep has a step to take for it
        started = time.monotonic()
        service.sweep(db_session)
        assert time.monotonic() - started < 2.0
        assert other.state != "provisioning"  # advanced
        assert idle.state == "active" and idle.end_reason == ""  # unknown: kept

    def test_the_batch_asks_each_backend_once(self, long_lab, db_session, mock_console, monkeypatch):
        mock_console.connections = 0
        labs = [TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)[0] for _ in range(1)]
        s2, _ = _try_launch(db_session, student(db_session), long_lab)
        until(db_session, s2, "ready")
        s2.idle_expires_at = labs[0].idle_expires_at
        db_session.commit()
        service.sweep(db_session)
        assert mock_console.logins == 1

    def test_no_console_lock_is_held_while_asking(self, long_lab, db_session, mock_console, monkeypatch):
        """The reader runs before the per-lab leases: none is held while it reads."""
        seen = []

        def reader(self, db, targets):
            def read():
                seen.extend(db_session.query(LabSession.lease_holder).filter(LabSession.lease_holder.isnot(None)).all())
                return {t.key: 0 for t in targets}

            return read

        monkeypatch.setattr(type(mock_console()), "console_reader", reader)
        TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)
        assert seen == []


class TestTheVsphereConsoleBatch:
    """One login per vCenter, with a connection timeout; one PropertyCollector read; a
    vCenter that does not answer in time is unknown and does not hold the others up."""

    def _fakes(self, monkeypatch, slow_host=None):
        import time
        from types import SimpleNamespace

        from app import hypervisor_binding
        from app.console_backends import vsphere_webmks as m

        logins = []

        class Spec(SimpleNamespace):
            pass

        def smart_connect(**kw):
            logins.append(kw)
            if kw["host"] == slow_host:
                time.sleep(3)

            def retrieve(specs, options):
                objs = [SimpleNamespace(obj=o.obj, propSet=[
                    SimpleNamespace(name="name", val=o.obj._moId),
                    SimpleNamespace(name="config.annotation", val="tn"),
                    SimpleNamespace(name="runtime.numMksConnections", val=1 if o.obj._moId == "vm-busy" else 0),
                ]) for o in specs[0].objectSet]
                return SimpleNamespace(objects=objs, token=None)

            pc = SimpleNamespace(RetrievePropertiesEx=retrieve, ContinueRetrievePropertiesEx=None)
            return SimpleNamespace(_stub=None, RetrieveContent=lambda: SimpleNamespace(propertyCollector=pc))

        monkeypatch.setattr(m, "Connect", smart_connect)
        monkeypatch.setattr(m, "_IN_FLIGHT", set())
        monkeypatch.setattr(m, "Disconnect", lambda si: None)
        monkeypatch.setattr(m, "vim", SimpleNamespace(VirtualMachine=lambda moid, stub: SimpleNamespace(_moId=moid)))
        monkeypatch.setattr(m, "vmodl", SimpleNamespace(query=SimpleNamespace(PropertyCollector=SimpleNamespace(
            FilterSpec=Spec, ObjectSpec=Spec, PropertySpec=Spec, RetrieveOptions=Spec))))
        monkeypatch.setattr(hypervisor_binding, "resolve", lambda db, rng, t: {
            "url": f"https://{rng.host}", "username": "u", "password": "p", "verify_ssl": True})
        monkeypatch.setattr(hypervisor_binding, "owns", lambda *a: True)
        return m, logins

    def _target(self, key, host, *vm_ids):
        from types import SimpleNamespace

        from app.console_backends import ConsoleTarget

        rid = uuid.uuid4()
        return ConsoleTarget(key, str(rid), [{"vm_id": v, "name": v} for v in vm_ids],
                             rng=SimpleNamespace(id=rid, host=host))

    def test_one_login_per_vcenter_with_a_timeout(self, monkeypatch):
        m, logins = self._fakes(monkeypatch)
        targets = [self._target("a", "vc1", "vm-1", "vm-busy"), self._target("b", "vc1", "vm-2"),
                   self._target("c", "vc2", "vm-3")]
        assert m.VsphereWebMKSConsole().console_reader(object(), targets)() == {"a": 1, "b": 0, "c": 0}
        assert sorted(kw["host"] for kw in logins) == ["vc1", "vc2"]
        assert all(kw["httpConnectionTimeout"] == m.CONNECT_TIMEOUT for kw in logins)
        assert all(kw["version"] == m.API_VERSION for kw in logins)  # no version probe

    def test_a_vcenter_that_does_not_answer_in_time_is_unknown(self, monkeypatch):
        import time

        m, _ = self._fakes(monkeypatch, slow_host="vc-slow")
        monkeypatch.setattr(m, "VCENTER_SECONDS", 0.3)
        targets = [self._target("fast", "vc1", "vm-busy"), self._target("slow", "vc-slow", "vm-9")]
        started = time.monotonic()
        answer = m.VsphereWebMKSConsole().console_reader(object(), targets)()
        assert time.monotonic() - started < 2.0
        assert answer == {"fast": 1, "slow": None}


class TestWhoCanTellAboutConsoles:
    def test_a_backend_with_no_console_concept_closes_on_heartbeats_alone(self, long_lab, db_session, monkeypatch):
        session, _ = TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        session.backend = "proxmox_api"  # no console adapter, not a console family
        db_session.commit()
        service.sweep(db_session)
        assert session.end_reason == "idle"

    def test_a_vsphere_lab_with_no_adapter_is_unknown_and_kept(self, long_lab, db_session, monkeypatch):
        from app import console_backends

        session, _ = TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        session.backend = "vsphere_api"
        db_session.commit()
        monkeypatch.delitem(console_backends._REGISTRY, "vsphere_api")
        service.sweep(db_session)
        assert session.state == "active" and session.end_reason == ""

    def test_unknown_is_still_bounded_by_the_maximum_lifetime(self, long_lab, db_session, mock_console,
                                                             monkeypatch):
        mock_console.connections = None
        session, t0 = TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(hours=13))
        service.sweep(db_session)
        assert session.end_reason == "expired"

    def test_the_platform_caps_a_profiles_lifetime(self, long_lab, db_session, monkeypatch):
        monkeypatch.setenv("LAB_MAX_LIFETIME_MINUTES", "60")  # the profile asks for 12 hours
        session, _ = _try_launch(db_session, student(db_session), long_lab)
        assert session.max_expires_at - session.provisioning_at <= timedelta(minutes=60, seconds=1)


class TestAClassLabFollowsItsClass:
    def test_back_to_back_sessions_keep_the_lab_to_the_last_one(self, lab, db_session, monkeypatch):  # noqa: F811
        """Round 3: the lab ended with the first of two back-to-back sessions of its class."""
        _, rid, course_id = lab
        now = datetime.now(UTC)
        a_end = now + timedelta(minutes=10)
        evt_a, cls = _lab_class(db_session, course_id, end=a_end)
        evt_b = ScheduledEvent(name="C304 serial 3 pm", tenant_id=cls.tenant_id, state=EventState.scheduled,
                               start_time=a_end + timedelta(minutes=5), end_time=a_end + timedelta(hours=3),
                               course_id=course_id, vcpu_total=8, ram_mb_total=8192, disk_gb_total=80)
        db_session.add(evt_b)
        db_session.flush()
        db_session.add(ClassSession(class_id=cls.id, event_id=evt_b.id, tenant_id=cls.tenant_id,
                                    range_mode="collective"))
        db_session.commit()
        s, _ = _try_launch(db_session, _on_roll_call(db_session, cls), rid)
        assert s.booking_id == evt_a.id
        assert s.max_expires_at.replace(tzinfo=UTC) == min(
            s.latest_end_at.replace(tzinfo=UTC), evt_b.end_time.replace(tzinfo=UTC) + TEARDOWN_GRACE)
        until(db_session, s, "ready")
        _at(monkeypatch, a_end + timedelta(minutes=6))  # A has ended: the lab moves to B
        act(db_session, service.advance, s)
        assert s.booking_id == evt_b.id and s.state in ("ready", "active")

    def test_editing_the_sessions_end_moves_the_labs_end(self, long_lab, db_session):
        """Round 3: an extended session left its labs on the old end; a shortened one too."""
        course_id = db_session.get(service.CourseRelease, long_lab).course_id
        now = datetime.now(UTC)
        evt, cls = _lab_class(db_session, course_id, end=now + timedelta(minutes=30))
        s, _ = _try_launch(db_session, _on_roll_call(db_session, cls), long_lab)
        until(db_session, s, "ready")
        evt.end_time = now + timedelta(hours=4)
        db_session.commit()
        act(db_session, service.advance, s)
        assert s.max_expires_at.replace(tzinfo=UTC) == evt.end_time.replace(tzinfo=UTC) + TEARDOWN_GRACE
        evt.end_time = now + timedelta(hours=1)
        db_session.commit()
        act(db_session, service.advance, s)
        assert s.max_expires_at.replace(tzinfo=UTC) == evt.end_time.replace(tzinfo=UTC) + TEARDOWN_GRACE

    def test_a_session_that_has_ended_is_never_drawn_on(self, lab, db_session):  # noqa: F811
        _, rid, course_id = lab
        now = datetime.now(UTC)
        evt, cls = _lab_class(db_session, course_id, end=now - timedelta(minutes=1))
        s, _ = _try_launch(db_session, _on_roll_call(db_session, cls), rid)
        assert s.booking_id is None


class TestTheBackstopKeepsTheLabsNetworks:
    """Round 4, B2: releasing them handed the class range's live port groups to the next lab."""

    def test_its_port_groups_stay_leased_for_an_operator(self, long_lab, db_session, worker, monkeypatch):  # noqa: F811
        from app.lab_sessions.models import LabNetworkLease

        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01,pg-lab-02")
        session, _ = _try_launch(db_session, student(db_session), long_lab)
        until(db_session, session, "ready")
        assert db_session.query(LabNetworkLease).filter(LabNetworkLease.session_id == session.id).count() == 1
        TestClassRangesAreNeverClosed()._class_range(db_session, session.range_id)
        _at(monkeypatch, datetime.now(UTC) + timedelta(hours=13))
        service.sweep(db_session)
        assert session.end_reason == "class_range"
        assert db_session.query(LabNetworkLease).filter(LabNetworkLease.session_id == session.id).count() == 1

    def test_another_student_never_gets_the_class_ranges_port_group(
        self, lab, db_session, worker, monkeypatch  # noqa: F811
    ):
        """The reviewer's repro: with one port group, the next lab must wait, not take it."""
        _, rid, _ = lab
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01")
        monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "1000")
        a, _ = _try_launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        pg_a = set(service._session_networks(db_session, a).values())
        TestClassRangesAreNeverClosed()._class_range(db_session, a.range_id)
        _at(monkeypatch, datetime.now(UTC) + timedelta(days=8))
        act(db_session, service.advance, a)
        assert a.end_reason == "class_range"
        monkeypatch.setattr(service, "_now", lambda: datetime.now(UTC))
        b, _ = _try_launch(db_session, student(db_session), rid)
        assert not pg_a & set(service._session_networks(db_session, b).values())
        assert b.state == "queued"


# ── round 4 (b743bfea) ───────────────────────────────────────────────


class TestConsoleThreadsGetPlainDataOnly:
    """Round 4, B1: an abandoned vCenter thread read ``t.rng.id`` after the sweep committed,
    a lazy load: SQL on the sweep's Session from another thread."""

    def test_no_sql_is_issued_from_a_console_thread(self, long_lab, db_session, monkeypatch):
        import threading
        import time
        from types import SimpleNamespace
        from unittest import mock

        from app.console_backends import vsphere_webmks as vw
        from sqlalchemy import event

        s = student(db_session)
        session, _ = _try_launch(db_session, s, long_lab)
        until(db_session, session, "ready")
        rng = db_session.get(Range, session.range_id)
        rng.provisioner_output = json.dumps({"vms": [{"vm_id": "vm-1", "name": f"{str(rng.id)[:8]}-ws"}]})
        session.backend = "vsphere_api"
        db_session.commit()
        t0 = datetime.now(UTC)
        _at(monkeypatch, t0)
        act(db_session, service.touch, session)
        _at(monkeypatch, t0 + timedelta(hours=4, minutes=1))
        monkeypatch.setenv("VSPHERE_URL", "https://vc.example")
        monkeypatch.setenv("LAB_CONSOLE_BATCH_SECONDS", "0.3")
        monkeypatch.setattr(vw, "VCENTER_SECONDS", 5)
        monkeypatch.setattr(vw, "_IN_FLIGHT", set())
        obj = SimpleNamespace(obj=SimpleNamespace(_moId="vm-1"), propSet=[
            SimpleNamespace(name="name", val=f"{str(rng.id)[:8]}-ws"),
            SimpleNamespace(name="runtime.numMksConnections", val=1)])
        si = mock.MagicMock()
        si.RetrieveContent.return_value.propertyCollector.RetrievePropertiesEx.return_value = SimpleNamespace(
            objects=[obj], token=None)
        monkeypatch.setattr(vw, "Connect", lambda **kw: (time.sleep(1.0), si)[1])  # answers after the deadline
        monkeypatch.setattr(vw, "Disconnect", lambda si: None)
        monkeypatch.setattr(vw, "vmodl", mock.MagicMock())
        monkeypatch.setattr(vw, "vim", mock.MagicMock())
        seen = []
        eng = db_session.get_bind()

        def listener(*a):
            seen.append(threading.current_thread().name)

        event.listen(eng, "before_cursor_execute", listener)
        try:
            service.sweep(db_session)
            time.sleep(2.0)  # the abandoned thread finishes
        finally:
            event.remove(eng, "before_cursor_execute", listener)
        assert [n for n in seen if n.startswith("tn-")] == []


class TestOneHungVcenter:
    """Round 4, S1: threads piled up on a hung vCenter, and it disabled idle-close everywhere."""

    def test_two_sweeps_start_at_most_one_thread_on_a_hung_vcenter(self, monkeypatch):
        import threading
        import time

        m, logins = TestTheVsphereConsoleBatch()._fakes(monkeypatch, slow_host="vc-slow")
        monkeypatch.setattr(m, "VCENTER_SECONDS", 0.2)
        target = TestTheVsphereConsoleBatch()._target("slow", "vc-slow", "vm-9")
        first = m.VsphereWebMKSConsole().console_reader(object(), [target])()
        second = m.VsphereWebMKSConsole().console_reader(object(), [target])()
        assert first == second == {"slow": None}
        assert len(logins) == 1  # the second check did not start another thread
        assert all(t.daemon for t in threading.enumerate() if t.name == "tn-console")
        time.sleep(3.2)  # the hung login comes back: the vCenter is asked again next time
        m.VsphereWebMKSConsole().console_reader(object(), [target])
        assert not m._IN_FLIGHT

    def test_a_healthy_vcenters_answer_survives_anothers_timeout_in_the_sweep(
        self, long_lab, db_session, mock_console, monkeypatch
    ):
        """The per-vCenter limit is shorter than the batch's: healthy answers come back."""
        from app.console_backends import vsphere_webmks as m

        monkeypatch.delenv("LAB_CONSOLE_VCENTER_SECONDS", raising=False)
        monkeypatch.setenv("LAB_CONSOLE_BATCH_SECONDS", "30")
        monkeypatch.setattr(m, "VCENTER_SECONDS", None)
        assert m.vcenter_seconds() < 30
        mock_console.connections = 1
        session, _ = TestConsoleUseIsActivity()._idle(db_session, long_lab, monkeypatch)
        service.sweep(db_session)
        assert session.state == "active" and session.end_reason == ""


class TestARemovedStudentsLabIsNotExtended:
    def test_taken_off_the_roll_call_the_lab_keeps_its_end_or_ends_sooner(self, long_lab, db_session):
        course_id = db_session.get(service.CourseRelease, long_lab).course_id
        now = datetime.now(UTC)
        evt, cls = _lab_class(db_session, course_id, end=now + timedelta(minutes=30))
        s = _on_roll_call(db_session, cls)
        lab_session, _ = _try_launch(db_session, s, long_lab)
        until(db_session, lab_session, "ready")
        before = lab_session.max_expires_at
        db_session.query(RollCallEntry).filter(RollCallEntry.user_id == s.id).update({"status": "removed"})
        evt.end_time = now + timedelta(hours=4)  # the class runs on: the removed Student's lab does not
        db_session.commit()
        act(db_session, service.advance, lab_session)
        assert lab_session.max_expires_at == before
