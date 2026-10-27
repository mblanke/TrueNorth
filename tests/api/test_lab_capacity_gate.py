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
        with pytest.raises(service.LabsBusyError, match=r"^Labs are busy, next free at \d\d:\d\d"):
            _try_launch(db_session, student(db_session), rid)
        db_session.rollback()
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
        """A lab already in the queue (behind a quota) is not refused; it waits, and says why."""
        _, rid, _ = lab
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
        session, _ = _try_launch(db_session, student(db_session), rid)
        assert session.state == "queued"
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")
        _booking(db_session, vcpu=10)
        act(db_session, service.advance, session)
        assert session.state == "queued" and session.error.startswith("Labs are busy, next free at")


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
        assert str(refused.value) == f"Labs are busy, next free at {_ceil15(closes):%H:%M}" or (
            # just before midnight UTC the next free time is tomorrow
            str(refused.value).startswith(f"Labs are busy, next free at {_ceil15(closes):%H:%M} on ")
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
            resp = integrations._labs_busy_page(str(exc))
        assert resp is not None and resp.status_code == 409
        body = resp.body.decode()
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
        act(db_session, service.advance, session)
        assert session.state == "active"
        _at(monkeypatch, t0 + timedelta(hours=4))
        act(db_session, service.advance, session)
        assert session.state in ("expired", "cleaning", "destroyed") and session.end_reason == "idle"

    def test_activity_keeps_it_open(self, long_lab, db_session, monkeypatch):
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(hours=3))
        act(db_session, service.touch, session)  # the lab page's heartbeat
        _at(monkeypatch, t0 + timedelta(hours=6))
        act(db_session, service.advance, session)
        assert session.state == "active"

    def test_the_idle_limit_is_a_setting(self, long_lab, db_session, monkeypatch):
        monkeypatch.setenv("LAB_IDLE_LIMIT_MINUTES", "30")
        session, _, t0 = self._active(db_session, long_lab, monkeypatch)
        _at(monkeypatch, t0 + timedelta(minutes=31))
        act(db_session, service.advance, session)
        assert session.end_reason == "idle"

    def test_a_shorter_idle_time_in_the_profile_still_applies(self, lab, db_session, monkeypatch):  # noqa: F811
        _, rid, _ = lab  # the kit's profile closes after 60 idle minutes
        session, _, t0 = self._active(db_session, rid, monkeypatch)
        _at(monkeypatch, t0 + timedelta(minutes=61))
        act(db_session, service.advance, session)
        assert session.end_reason == "idle"

    def test_the_student_is_warned_once_before_it_closes(self, long_lab, db_session, monkeypatch):
        session, s, t0 = self._active(db_session, long_lab, monkeypatch)

        def warnings():
            return db_session.query(UserNotification).filter(UserNotification.user_id == s.id).all()

        _at(monkeypatch, t0 + timedelta(hours=3, minutes=40))
        act(db_session, service.advance, session)
        assert warnings() == []
        _at(monkeypatch, t0 + timedelta(hours=3, minutes=46))
        act(db_session, service.advance, session)
        _at(monkeypatch, t0 + timedelta(hours=3, minutes=50))
        act(db_session, service.advance, session)
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
            act(db_session, service.advance, session)
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
        assert session.state == "ready" and "belongs to a class" in session.error
        assert db_session.get(Range, session.range_id).state == RangeState.ready
