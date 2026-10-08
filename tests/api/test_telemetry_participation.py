"""A Student searches only telemetry they are part of (security sweep M2).

``GET /telemetry/{range_id}/search`` checked sign-in and tenant only: any Student read any
range of the tenant, other Students' labs included. Now a Student reads their own lab's
range and the range of a running exercise they take part in (app/telemetry_access.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app import main as app_main
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.lab_sessions.models import LabSession
from app.models import Course, Enrollment, EnrollmentStatus, ExerciseState, Range, Template, UserRole
from app.scheduler.models import EventState, ScheduledEvent


class _Backend:
    def __init__(self):
        self.searched: list[str] = []

    async def search(self, index, query, size):
        self.searched.append(index)
        return {"hits": {"hits": []}}


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(app_main, "get_search_backend", lambda: b)
    return b


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "tel-a"), real_tenant(db_session, "tel-b")
    return {
        "a": a, "b": b,
        "student": real_user(db_session, UserRole.student, a.id),
        "classmate": real_user(db_session, UserRole.student, a.id),
        "instructor": real_user(db_session, UserRole.instructor, a.id),
        "range_ops": real_user(db_session, UserRole.range_ops, a.id),
        "foreign_student": real_user(db_session, UserRole.student, b.id),
    }


def _search(client, range_id):
    return client.get(f"/telemetry/{range_id}/search", params={"q": "*"})


def _lab(db, owner, tenant_id) -> uuid.UUID:
    t = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant_id)
    db.add(t)
    db.flush()
    rng = Range(id=uuid.uuid4(), name="lab", template_id=t.id, tenant_id=tenant_id)
    course = Course(id=uuid.uuid4(), name="C", tenant_id=tenant_id)
    sha = uuid.uuid4().hex * 2
    db.add_all([rng, course, CourseReleaseBlob(sha256=sha, size=1, data=b"x")])
    db.flush()
    rel = CourseRelease(id=uuid.uuid4(), tenant_id=tenant_id, course_id=course.id, catalogue_code="c", arc2_code="a",
                        run_id="r", slug="s", title="T", version=1, release_digest=sha, learner_digest=sha,
                        platform_digest=sha, instructor_digest=sha, blob_sha256=sha, meta="{}")
    db.add(rel)
    db.flush()
    db.add(LabSession(tenant_id=tenant_id, user_id=uuid.UUID(owner.id), release_id=rel.id, activity_id="mod_001",
                      state="ready", profile_id="p", profile_digest="0" * 64, backend="mock", range_id=rng.id))
    db.flush()
    return rng.id


def test_a_student_reads_their_own_lab_not_a_classmates(client, db_session, backend, world):
    mine = _lab(db_session, world["student"], world["a"].id)
    act_as(world["student"])
    assert _search(client, mine).status_code == 200
    act_as(world["classmate"])
    assert _search(client, mine).status_code == 404
    assert backend.searched == [f"range-{mine}"]


def test_a_student_reads_a_running_exercises_range_not_an_idle_one(client, db_session, backend, world):
    running = real_exercise(db_session, world["a"].id, state=ExerciseState.running)
    idle = real_exercise(db_session, world["a"].id, state=ExerciseState.completed)
    act_as(world["student"])
    assert _search(client, running.range_id).status_code == 200
    assert _search(client, idle.range_id).status_code == 404


def test_a_booked_exercise_is_for_its_course_students_only(client, db_session, backend, world):
    ex = real_exercise(db_session, world["a"].id, state=ExerciseState.running)
    course = Course(id=uuid.uuid4(), name="Class", tenant_id=world["a"].id)
    db_session.add(course)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add(ScheduledEvent(name="lesson", tenant_id=world["a"].id, range_id=ex.range_id, exercise_id=ex.id,
                                  course_id=course.id, state=EventState.active, start_time=now,
                                  end_time=now + timedelta(hours=2)))
    db_session.add(Enrollment(user_id=uuid.UUID(world["student"].id), course_id=course.id,
                              tenant_id=world["a"].id, status=EnrollmentStatus.enrolled))
    db_session.flush()

    act_as(world["student"])
    assert _search(client, ex.range_id).status_code == 200
    act_as(world["classmate"])  # same tenant, not on the course
    assert _search(client, ex.range_id).status_code == 404


def test_another_tenants_student_never_reads(client, db_session, backend, world):
    ex = real_exercise(db_session, world["a"].id, state=ExerciseState.running)
    act_as(world["foreign_student"])
    assert _search(client, ex.range_id).status_code == 404


def test_staff_read_tenant_ranges_and_only_infra_staff_read_labs(client, db_session, backend, world):
    idle = real_exercise(db_session, world["a"].id, state=ExerciseState.completed)
    lab = _lab(db_session, world["student"], world["a"].id)
    observer = real_user(db_session, UserRole.observer, world["a"].id)
    act_as(observer)
    assert _search(client, idle.range_id).status_code == 200
    assert _search(client, lab).status_code == 404  # as _readable_range: labs need infra:read
    for who in (world["instructor"], world["range_ops"]):
        act_as(who)
        assert _search(client, lab).status_code == 200
