"""Detection credit and the exercise lifecycle on Postgres built by Alembic (review finding 7).

The other real-DB tests build the schema with ``Base.metadata.create_all`` on SQLite.
Production runs the Alembic chain on Postgres, where ``exercises.state`` is a native enum
(the worker compares it through a CAST), foreign keys are enforced, and the migrations
that add ``detection_submissions`` and rewrite validator names must actually run.

Set TEST_POSTGRES_ADMIN_URL to a superuser URL, e.g.
postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres. The module creates and
drops its own scratch database. CI's test-python job sets it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from _shared import SCENARIO, FakeStore, _run_upgrade_head, acting_as, beacon, noise
from sqlalchemy.orm import Session, sessionmaker

ADMIN_URL = os.getenv("TEST_POSTGRES_ADMIN_URL")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not ADMIN_URL, reason="set TEST_POSTGRES_ADMIN_URL to run against Postgres"),
]


@pytest.fixture(scope="module")
def pg_url():
    name = f"tn_detect_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
        _run_upgrade_head(url)
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def pg(pg_url, monkeypatch):
    pytest.importorskip("worker.celery_app")  # first: importing worker.tasks first hits a cycle on main
    tasks = pytest.importorskip("worker.tasks")
    engine = sa.create_engine(pg_url)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_notify_api", MagicMock())
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    db = factory()
    yield db, factory
    db.close()
    engine.dispose()


def _world(db, *, started_minutes_ago=5, scenario_yaml=SCENARIO, validator="validate.opensearch.query"):
    from app import models as m

    suffix = uuid.uuid4().hex[:8]
    tenant = m.Tenant(name=f"t-{suffix}", slug=f"t-{suffix}")
    db.add(tenant)
    db.flush()
    tmpl = m.Template(name="tpl", yaml="name: tpl\n", tenant_id=tenant.id)
    db.add(tmpl)
    db.flush()
    rng = m.Range(name="r", template_id=tmpl.id, tenant_id=tenant.id, provisioner_backend="vsphere_api",
                  state=m.RangeState.ready)
    sc = m.Scenario(name="s", yaml=scenario_yaml, tenant_id=tenant.id)
    student = m.User(keycloak_id=f"kc-{suffix}", email=f"s-{suffix}@example.org", display_name="Student One",
                     tenant_id=tenant.id, role=m.UserRole.student)
    db.add_all([rng, sc, student])
    db.flush()
    ex = m.Exercise(name="e", range_id=rng.id, scenario_id=sc.id, tenant_id=tenant.id, state=m.ExerciseState.running,
                    started_at=datetime.now(UTC) - timedelta(minutes=started_minutes_ago))
    db.add(ex)
    db.flush()
    db.add(m.Objective(exercise_id=ex.id, ref_id="detect_c2", objective_type=m.ObjectiveType.detection,
                       description="c2", validator=validator, points=40))
    db.commit()
    return tenant, ex, student


def _participant(db, tenant, ex, student):
    """Make ``student`` a participant (ADR 0005): a live booking ties the exercise to a
    course, and the Student is enrolled in it."""
    from app import models as m
    from app.scheduler.models import EventState, ScheduledEvent

    course = m.Course(name="Class", tenant_id=tenant.id)
    db.add(course)
    db.flush()
    now = datetime.now(UTC)
    db.add(ScheduledEvent(name="lesson", tenant_id=tenant.id, exercise_id=ex.id, range_id=ex.range_id,
                          course_id=course.id, state=EventState.active, start_time=now - timedelta(hours=1),
                          end_time=now + timedelta(hours=1)))
    db.add(m.Enrollment(user_id=student.id, course_id=course.id, tenant_id=tenant.id,
                        status=m.EnrollmentStatus.enrolled))
    db.commit()


def test_the_chain_creates_detection_submissions(pg):
    db, _ = pg
    cols = {c["name"] for c in sa.inspect(db.get_bind()).get_columns("detection_submissions")}
    assert {"exercise_id", "objective_ref", "user_id", "query", "verdict", "on_target", "window_start"} <= cols


def test_a_student_detection_is_credited_on_postgres(pg, monkeypatch):
    from app.db import get_db
    from app.main import app as fastapi_app
    from app.models import Exercise, Objective, UserRole
    from app.routers.detections import search_backend
    from fastapi.testclient import TestClient

    db, factory = pg
    tenant, ex, student = _world(db)
    _participant(db, tenant, ex, student)
    start = ex.started_at
    store = FakeStore([beacon(), beacon(), *noise(n=1)])
    for e in store.events:  # the fake store's window is relative to the exercise start
        e["truenorth"]["ingested_at"] = (start + timedelta(minutes=1)).isoformat()

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _db
    fastapi_app.dependency_overrides[search_backend] = lambda: store
    try:
        with acting_as(UserRole.student, tenant=str(tenant.id)) as who:
            who.id = str(student.id)  # foreign keys are enforced here
            resp = TestClient(fastapi_app).post(
                f"/exercises/{ex.id}/objectives/detect_c2/detections", json={"query": "url.domain:*northwind*"}
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        fastapi_app.dependency_overrides.pop(search_backend, None)

    assert resp.status_code == 201, resp.text
    assert resp.json()["verdict"] == "achieved"
    db.expire_all()
    obj = db.query(Objective).filter(Objective.exercise_id == ex.id).one()
    assert obj.achieved and json.loads(obj.evidence)["student_id"] == str(student.id)
    assert db.get(Exercise, ex.id).total_score == 40


def test_worker_guards_and_api_clock_work_against_the_native_enum(pg, monkeypatch):
    from app import celery_client
    from app import models as m
    from app.exercise_completion import sweep_overdue
    from worker import db_ops

    monkeypatch.setattr(celery_client, "dispatch", lambda *a: "id")
    db, _ = pg
    _, overdue, _ = _world(db, started_minutes_ago=200, scenario_yaml="duration_minutes: 90\n" + SCENARIO)
    _, live, _ = _world(db, started_minutes_ago=5, scenario_yaml="duration_minutes: 90\n" + SCENARIO)
    _, closed, _ = _world(db, started_minutes_ago=5)
    closed.state = m.ExerciseState.cancelled
    db.commit()

    assert overdue.id in set(sweep_overdue(db))
    with db_ops_session(pg) as s:
        db_ops.achieve_objective(s, str(closed.id), "detect_c2", evidence="late")
        db_ops.complete_exercise(s, str(closed.id))
        db_ops.start_exercise(s, str(closed.id))
    db.expire_all()
    assert db.get(m.Exercise, overdue.id).state == m.ExerciseState.completed
    assert db.get(m.Exercise, live.id).state == m.ExerciseState.running
    assert db.get(m.Exercise, closed.id).state == m.ExerciseState.cancelled
    assert db.query(m.Objective).filter(m.Objective.exercise_id == closed.id).one().achieved is False


class _Slow(FakeStore):
    async def match(self, index, query, size=0):
        await asyncio.sleep(0.2)
        return await super().match(index, query, size)


def test_concurrent_submissions_cannot_exceed_the_attempt_cap(pg, monkeypatch):
    # Review M1: twenty parallel submissions all saw "0 used" and were all judged.
    import time

    from app.db import get_db
    from app.main import app as fastapi_app
    from app.models import UserRole
    from app.routers import detections
    from app.routers.detections import search_backend
    from fastapi.testclient import TestClient

    counted = detections._attempts

    def slow_count(*args):  # widen the count-then-insert window so a missing lock shows
        out = counted(*args)
        time.sleep(0.05)
        return out

    monkeypatch.setattr(detections, "_attempts", slow_count)

    db, factory = pg
    tenant, ex, student = _world(db)
    _participant(db, tenant, ex, student)

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _db
    fastapi_app.dependency_overrides[search_backend] = lambda: _Slow([])
    try:
        with acting_as(UserRole.student, tenant=str(tenant.id)) as who:
            who.id = str(student.id)

            def submit(_):  # no lifespan: each thread is one more client of the same app
                c = TestClient(fastapi_app)
                return c.post(f"/exercises/{ex.id}/objectives/detect_c2/detections", json={"query": "nope:x"})

            with ThreadPoolExecutor(max_workers=12) as pool:
                codes = sorted(r.status_code for r in pool.map(submit, range(12)))
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        fastapi_app.dependency_overrides.pop(search_backend, None)
    assert codes.count(201) == 5 and codes.count(429) == 7, codes


@contextmanager
def db_ops_session(pg):
    _, factory = pg
    s = factory()
    try:
        yield s
        s.commit()
    finally:
        s.close()
