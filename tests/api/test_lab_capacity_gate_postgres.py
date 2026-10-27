"""The self-serve lab capacity gate under concurrency, on PostgreSQL (ADR 0014 §18, #288).
Skipped without TEST_POSTGRES_ADMIN_URL.

Capacity is site-wide, so the per-tenant lock a launch takes for its quotas (CR1-12) does
not serialise two Students of different tenants. Both would read the cluster with neither
lab written and both start, past what is left after approved classes. The gate takes the
scheduler's booking lock, so the last slot goes to one of them and the other is refused.
"""

from __future__ import annotations

import threading
import uuid

from app.course_releases.models import CourseRelease
from app.lab_sessions import service
from app.lab_sessions.models import LabSession
from app.models import Course, Tenant, User, UserRole
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from test_lab_lifecycle_postgres import DEV, pg_lab  # noqa: F401 — the fixture

OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000ee")


def _copy(row, **changes):
    cls = type(row)
    values = {a.key: getattr(row, a.key) for a in inspect(cls).column_attrs}
    values.update(changes)
    return cls(**values)


def _release_in_other_tenant(engine, release_id: uuid.UUID) -> uuid.UUID:
    """The same accepted release (same stored bundle) in a second tenant."""
    with Session(engine) as s:
        s.add(Tenant(id=OTHER, name="other", slug="other-pg"))
        s.flush()
        rel = s.get(CourseRelease, release_id)
        course = _copy(s.get(Course, rel.course_id), id=uuid.uuid4(), tenant_id=OTHER)
        s.add(course)
        s.flush()
        copy = _copy(rel, id=uuid.uuid4(), tenant_id=OTHER, course_id=course.id)
        s.add(copy)
        s.commit()
        return copy.id


def _student(engine, tenant: uuid.UUID) -> uuid.UUID:
    with Session(engine) as s:
        u = User(email=f"s{uuid.uuid4().hex[:8]}@x.test", display_name="s", role=UserRole.student,
                 tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
        s.add(u)
        s.commit()
        return u.id


def test_on_postgres_two_tenants_starting_the_last_slot_at_once_get_one_lab(
    postgres_engine, pg_lab, monkeypatch  # noqa: F811
):
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "3")  # one 2-vCPU lab fits, not two
    monkeypatch.setattr(service, "_dispatch", lambda task, *args: "task-1")
    other_release = _release_in_other_tenant(postgres_engine, pg_lab)
    starts = [(DEV, _student(postgres_engine, DEV), pg_lab),
              (OTHER, _student(postgres_engine, OTHER), other_release)]
    barrier = threading.Barrier(len(starts))
    busy: list[str] = []
    errors: list[BaseException] = []

    def start(tenant, user_id, release_id):
        try:
            with Session(postgres_engine) as db:
                barrier.wait()
                try:
                    with db.begin_nested():
                        service.launch(db, tenant_id=tenant, user_id=user_id, release_id=release_id,
                                       activity_id="mod_006")
                except service.LabsBusyError as exc:
                    db.info.pop(service.OUTBOX, None)
                    db.rollback()
                    busy.append(str(exc))
                    return
                db.commit()
                service.flush_outbox(db)
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=start, args=a) for a in starts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    with Session(postgres_engine) as s:
        states = [x.state for x in s.query(LabSession).all()]
    assert states == ["provisioning"], f"one lab fits: {states}"
    assert len(busy) == 1 and busy[0].startswith("Labs are busy")
