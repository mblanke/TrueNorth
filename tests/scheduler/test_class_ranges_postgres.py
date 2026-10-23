"""A class's ranges on PostgreSQL: two clock replicas build each seat once (#282).

SQLite serialises writers, so a race there proves nothing. These run only with
TEST_POSTGRES_ADMIN_URL (CI's test-python job; ``postgres_engine`` in tests/conftest.py is a
fresh copy of ``alembic upgrade head``).

Every API replica runs the scheduler's clock, and with it the class ranges step. Two ticks at
the provisioning lead at once must still give each seat one ``class_ranges`` row (its unique
key) and one built range (the guarded claim of the row's ``range_id``): the replica that loses
discards the range it made. The teardown when the class ends also happens once. Two
roll-call additions at once leave an individual session sized from the roll call (seat
size times Students), whichever commits first (#299 review item 5).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from app.classes import ranges as class_ranges
from app.classes.models import Class, ClassRange, ClassSession, RollCallEntry
from app.models import Course, Range, RangeState, Template, Tenant, User, UserRole
from app.range_ops.models import RangeOperation
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

NINE = (datetime.now(UTC) + timedelta(days=20)).replace(hour=9, minute=0, second=0, microsecond=0)
LEAD, GRACE = timedelta(minutes=30), timedelta(minutes=15)
SEAT = "nodes:\n  - id: kali\n    os: kali\n    specs: {cores: 2, memory_mb: 4096, disk_gb: 40}\n"


@pytest.fixture
def world(postgres_engine):
    eng = postgres_engine
    tenant, course, lead, tmpl, cls, evt = (uuid.uuid4() for _ in range(6))
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        db.add(Course(id=course, name="SOC", tenant_id=tenant))
        db.add(User(id=lead, email="lead@corp.test", display_name="Lead", role=UserRole.instructor, tenant_id=tenant,
                    keycloak_id=f"kc-{lead.hex[:8]}"))
        db.add(Template(id=tmpl, name="seat", yaml=SEAT, tenant_id=tenant))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C201", course_id=course, start_date=date.today(),
                     end_date=NINE.date(), lead_instructor_id=lead, status="open"))
        db.flush()
        for n in ("ann", "bob", "cat"):
            db.add(RollCallEntry(class_id=cls, tenant_id=tenant, email=f"{n}@corp.test", status="invited"))
        db.add(ScheduledEvent(id=evt, name="Lab", state=EventState.scheduled, tenant_id=tenant, template_id=tmpl,
                              instructor_id=lead, start_time=NINE, end_time=NINE + timedelta(hours=3),
                              vcpu_total=6))
        db.flush()
        db.add(ClassSession(class_id=cls, event_id=evt, tenant_id=tenant, range_mode="individual"))
        db.commit()
    return {"engine": eng, "class": cls, "event": evt}


def _tick(eng, now, barrier: threading.Barrier | None = None, errors: list | None = None) -> None:
    db = Session(eng, autoflush=False)
    try:
        if barrier is not None:
            barrier.wait(timeout=10)
        class_ranges.tick(db, now)
    except Exception as exc:  # noqa: BLE001 — surfaced by the test
        if errors is not None:
            errors.append(exc)
        raise
    finally:
        db.close()


def test_concurrent_seat_changes_lose_no_update(world):
    """resize and resequence increment in SQL: two transactions that each read the booking,
    then change it, both count (a Python read-modify-write lost one; #298 review)."""
    from app.scheduler import service
    from app.scheduler.capacity import Resources

    eng = world["engine"]
    read = threading.Barrier(2)
    errors: list = []

    def take(sign: int):
        try:
            with Session(eng) as db:
                evt = db.get(ScheduledEvent, world["event"])
                _ = (evt.vcpu_total, evt.sequence)  # both have read the old values
                read.wait(timeout=10)
                service.resize(db, evt, Resources(4, 1024, 10), sign, vms=2)
                service.resequence(evt)
                db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=take, args=(s,)) for s in (1, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    with Session(eng) as db:
        evt = db.get(ScheduledEvent, world["event"])
        assert (evt.vcpu_total, evt.ram_mb_total, evt.vm_count, evt.sequence) == (6 + 8, 2048, 4, 2)
        service.resize(db, evt, Resources(100, 0, 0), -1)  # never below zero
        db.commit()
        db.refresh(evt)
        assert evt.vcpu_total == 0


def test_two_replicas_build_each_seat_once_and_tear_it_down_once(world):
    eng = world["engine"]
    sent: list[tuple[str, tuple]] = []
    lock = threading.Lock()

    def fake(name, *args):
        with lock:
            sent.append((name, args))
            return f"task-{len(sent)}"

    errors: list = []
    with patch("app.celery_client.dispatch", side_effect=fake):
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=_tick, args=(eng, NINE - LEAD, barrier, errors)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors

        with Session(eng) as db:
            rows = db.query(ClassRange).filter(ClassRange.class_id == world["class"]).all()
            assert len(rows) == 3 and all(r.range_id and r.builds == 1 for r in rows)
            built = [args[0] for name, args in sent if name == "provision_range"]
            assert sorted(built) == sorted(str(r.range_id) for r in rows)
            # The loser's ranges were rolled back with its claim: no orphan range rows.
            ranges = db.query(Range).filter(Range.template_id == rows[0].template_id).all()
            assert {r.id for r in ranges} == {r.range_id for r in rows}
            for r in ranges:
                r.state = RangeState.ready  # the worker reports the builds done
            db.query(ScheduledEvent).filter(ScheduledEvent.id == world["event"]).update(
                {ScheduledEvent.state: EventState.completed}, synchronize_session=False)
            db.commit()

        after = datetime.combine(NINE.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC) + GRACE
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=_tick, args=(eng, after, barrier, errors)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors

    with Session(eng) as db:
        rows = db.query(ClassRange).filter(ClassRange.class_id == world["class"]).all()
        assert all(r.torn_down_at is not None and r.teardown_reason == "class_ended" for r in rows)
        destroys = db.query(RangeOperation).filter(
            RangeOperation.range_id.in_([r.range_id for r in rows]), RangeOperation.action == "destroy").all()
        assert len(destroys) == 3 and {d.window_bypass for d in destroys} == {"booking_ended"}


def test_concurrent_additions_leave_the_size_equal_to_the_roll_call(world, monkeypatch):
    for k, v in (("CLUSTER_TOTAL_VCPU", "1000"), ("CLUSTER_TOTAL_RAM_MB", str(1000 * 1024)),
                 ("CLUSTER_TOTAL_DISK_GB", "10000"), ("CLUSTER_OVERHEAD_PCT", "0")):
        monkeypatch.setenv(k, v)
    from app.auth import CurrentUser
    from app.classes import service as class_service
    from app.classes.models import RollCallSettings
    from app.scheduler.models import BookingApproval

    eng = world["engine"]
    with Session(eng) as db:
        cls = db.get(Class, world["class"])
        db.add(RollCallSettings(tenant_id=cls.tenant_id, email_domains=["corp.test"]))
        db.add(BookingApproval(event_id=world["event"], tenant_id=cls.tenant_id))
        lead = db.get(User, cls.lead_instructor_id)
        actor = CurrentUser(id=str(lead.id), email=lead.email, display_name="Lead", role=UserRole.instructor,
                            tenant_id=str(cls.tenant_id), keycloak_id=lead.keycloak_id)
        db.commit()
    go = threading.Barrier(2)
    errors: list = []

    def add(email: str):
        try:
            with Session(eng, autoflush=False) as db:
                cls = db.get(Class, world["class"])
                go.wait(timeout=10)
                class_service.add_to_roll_call(db, cls, actor, [email])
                db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(f"{n}@corp.test",)) for n in ("dan", "eve")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    with Session(eng) as db:
        evt = db.get(ScheduledEvent, world["event"])
        assert (evt.vcpu_total, evt.vm_count) == (2 * 5, 1 * 5)  # five Students, one 2-vCPU VM each
