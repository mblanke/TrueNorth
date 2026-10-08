"""``app.enrollment``: the one place a Student gets enrolled.

Three callers share it (course enroll, registration approval, onboarding path
selection), so its guarantees are pinned here rather than through any one of them:

- idempotent: a second call returns the same row and adds no progress rows;
- one ``not_started`` ModuleProgress per module, created with the enrollment;
- flushes, never commits: the caller owns the transaction;
- a course's accepted release is pinned once, at first enrollment;
- learning paths resolve in their stored order and skip ids that are malformed,
  missing, or a path whose ``course_ids`` is not a JSON list.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app import enrollment as enrollment_mod
from app.enrollment import courses_for_learning_path, ensure_enrollment, ensure_path_enrollment
from app.models import (
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    LearningPath,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    Tenant,
    User,
    UserRole,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000e001")


@pytest.fixture
def tenant(db_session):
    if db_session.get(Tenant, TENANT) is None:
        db_session.add(Tenant(id=TENANT, name="enrol", slug="enrol"))
        db_session.flush()
    return TENANT


@pytest.fixture
def student(db_session, tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Student",
        role=UserRole.student,
        tenant_id=tenant,
    )
    db_session.add(u)
    db_session.flush()
    return u


def _course(db, modules: int = 2, name: str = "SOC 101") -> Course:
    course = Course(name=name, tenant_id=TENANT)
    db.add(course)
    db.flush()
    # Inserted out of order on purpose; progress rows follow ordinal, not insertion.
    for ordinal in reversed(range(modules)):
        db.add(
            CourseModule(
                course_id=course.id,
                ordinal=ordinal,
                title=f"M{ordinal}",
                content_type=ModuleContentType.reading,
            )
        )
    db.flush()
    return course


def _progress(db, enrollment: Enrollment) -> list[ModuleProgress]:
    return db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id).all()


# ── ensure_enrollment ───────────────────────────────────────────────────


def test_creates_enrollment_and_one_progress_row_per_module(db_session, student):
    course = _course(db_session, modules=3)
    e = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)

    assert e.id is not None  # flushed
    assert e.status == EnrollmentStatus.enrolled
    assert e.tenant_id == TENANT
    rows = _progress(db_session, e)
    assert len(rows) == 3
    assert {r.status for r in rows} == {ModuleProgressStatus.not_started}
    module_ids = {m.id for m in db_session.query(CourseModule).filter(CourseModule.course_id == course.id)}
    assert {r.module_id for r in rows} == module_ids


def test_is_idempotent(db_session, student):
    course = _course(db_session)
    first = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    first.status = EnrollmentStatus.in_progress  # progress made between the two calls
    db_session.flush()
    second = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)

    assert second.id == first.id
    assert second.status == EnrollmentStatus.in_progress  # existing row returned unchanged
    assert len(_progress(db_session, first)) == 2
    assert (
        db_session.query(Enrollment)
        .filter(Enrollment.user_id == student.id, Enrollment.course_id == course.id)
        .count()
        == 1
    )


def test_accepts_tenant_id_as_string(db_session, student):
    course = _course(db_session, modules=0)
    e = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=str(TENANT))
    db_session.expire(e)
    assert e.tenant_id == TENANT


def test_course_without_modules_gets_no_progress_rows(db_session, student):
    course = _course(db_session, modules=0)
    e = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    assert _progress(db_session, e) == []


def test_does_not_commit(db_session, student, monkeypatch):
    def _no_commit():
        raise AssertionError("ensure_enrollment must not commit; the caller owns the transaction")

    monkeypatch.setattr(db_session, "commit", _no_commit)
    course = _course(db_session)
    ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)


def test_rolls_back_with_the_callers_transaction(db_session, student):
    course = _course(db_session)
    savepoint = db_session.begin_nested()
    e = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    enrollment_id = e.id
    savepoint.rollback()
    assert db_session.get(Enrollment, enrollment_id) is None
    assert db_session.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment_id).count() == 0


def test_pins_the_release_once_for_a_new_enrollment_only(db_session, student, monkeypatch):
    from app.course_releases import service

    pinned: list[uuid.UUID] = []
    monkeypatch.setattr(service, "pin_enrollment", lambda _db, e: pinned.append(e.id))

    course = _course(db_session)
    e = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    assert pinned == [e.id]


def test_two_students_on_one_course_get_separate_progress(db_session, student, tenant):
    other = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Other",
        role=UserRole.student,
        tenant_id=tenant,
    )
    db_session.add(other)
    course = _course(db_session)
    a = ensure_enrollment(db_session, user_id=student.id, course_id=course.id, tenant_id=TENANT)
    b = ensure_enrollment(db_session, user_id=other.id, course_id=course.id, tenant_id=TENANT)
    assert a.id != b.id
    assert len(_progress(db_session, a)) == len(_progress(db_session, b)) == 2


# ── learning paths ──────────────────────────────────────────────────────


def _path(db, course_ids) -> LearningPath:
    raw = course_ids if isinstance(course_ids, str) else json.dumps([str(c) for c in course_ids])
    path = LearningPath(name="Analyst path", tenant_id=TENANT, course_ids=raw)
    db.add(path)
    db.flush()
    return path


def test_path_courses_keep_their_stored_order(db_session, tenant):
    a, b, c = (_course(db_session, modules=0, name=n) for n in ("A", "B", "C"))
    path = _path(db_session, [c.id, a.id, b.id])
    assert [x.name for x in courses_for_learning_path(db_session, path.id, tenant_id=TENANT)] == ["C", "A", "B"]


def test_path_skips_malformed_and_missing_ids(db_session, tenant, caplog):
    a = _course(db_session, modules=0, name="A")
    path = _path(db_session, json.dumps(["not-a-uuid", str(uuid.uuid4()), str(a.id), None, 7]))
    with caplog.at_level("WARNING", logger=enrollment_mod.logger.name):
        courses = courses_for_learning_path(db_session, path.id, tenant_id=TENANT)
    assert [x.id for x in courses] == [a.id]
    assert "malformed course id" in caplog.text
    assert "missing or foreign course" in caplog.text


@pytest.mark.parametrize("raw", ["{not json", '{"a": 1}', '"just a string"'])
def test_path_with_unusable_course_ids_resolves_to_nothing(db_session, tenant, raw):
    path = _path(db_session, raw)
    assert courses_for_learning_path(db_session, path.id, tenant_id=TENANT) == []


def test_path_with_empty_course_ids(db_session, tenant):
    path = _path(db_session, "")
    assert courses_for_learning_path(db_session, path.id, tenant_id=TENANT) == []


def test_unknown_path_resolves_to_nothing(db_session):
    assert courses_for_learning_path(db_session, uuid.uuid4(), tenant_id=TENANT) == []


def test_another_tenants_path_and_courses_are_never_enrolled(db_session, student):
    """Security sweep M4: registration passes a path id the applicant chose, and a path's
    course list is JSON with no foreign key."""
    other = uuid.UUID("00000000-0000-0000-0000-00000000e0b2")
    db_session.add(Tenant(id=other, name="other", slug="other-e0b2"))
    db_session.flush()
    mine = _course(db_session, modules=1, name="Mine")
    theirs = Course(id=uuid.uuid4(), name="Theirs", tenant_id=other)
    shared = Course(id=uuid.uuid4(), name="Shared", tenant_id=None)
    db_session.add_all([theirs, shared])
    db_session.flush()

    foreign_path = LearningPath(name="Their path", tenant_id=other, course_ids=json.dumps([str(theirs.id)]))
    db_session.add(foreign_path)
    db_session.flush()
    assert ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=foreign_path.id, tenant_id=TENANT) == []

    mixed = _path(db_session, [mine.id, theirs.id, shared.id])
    enrolled = ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=mixed.id, tenant_id=TENANT)
    assert [e.course_id for e in enrolled] == [mine.id, shared.id]
    assert db_session.query(Enrollment).filter(Enrollment.course_id == theirs.id).count() == 0


def test_path_enrollment_enrolls_every_course_and_is_idempotent(db_session, student):
    a = _course(db_session, modules=1, name="A")
    b = _course(db_session, modules=2, name="B")
    path = _path(db_session, [a.id, b.id])

    first = ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=path.id, tenant_id=TENANT)
    again = ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=path.id, tenant_id=TENANT)

    assert [e.course_id for e in first] == [a.id, b.id]
    assert [e.id for e in again] == [e.id for e in first]
    assert db_session.query(Enrollment).filter(Enrollment.user_id == student.id).count() == 2
    assert sum(len(_progress(db_session, e)) for e in first) == 3


def test_path_enrollment_reuses_an_existing_course_enrollment(db_session, student):
    a = _course(db_session, modules=1, name="A")
    b = _course(db_session, modules=1, name="B")
    direct = ensure_enrollment(db_session, user_id=student.id, course_id=a.id, tenant_id=TENANT)
    path = _path(db_session, [a.id, b.id])
    via_path = ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=path.id, tenant_id=TENANT)
    assert via_path[0].id == direct.id


def test_path_enrollment_on_unknown_path_enrolls_nothing(db_session, student):
    assert ensure_path_enrollment(db_session, user_id=student.id, learning_path_id=uuid.uuid4(), tenant_id=TENANT) == []
