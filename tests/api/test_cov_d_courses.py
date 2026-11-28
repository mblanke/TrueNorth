# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Courses (``app.routers.courses``): import guards, enrolment outcomes, catalogue filters.

Complements test_course_catalogue_visibility.py (tenancy and draft visibility),
test_programme_ingest.py and test_course_content_ingest.py (the ingest engines and the
programme import's status codes) with the behaviours those leave unasserted: the
course-content import's own guards (with the reason in the body), the enrolment state
machine (idempotent re-enrolment, the assigned upgrade of a self row, staff reopening a
withdrawn Student, failed/completed outcomes never reset, a retired course refusing
enrolment), withdraw's guards and idempotence, and the catalogue's tag AND / retired /
difficulty filters.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime

from _shared import DEV_TENANT
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    AuditLog,
    Course,
    Enrollment,
    EnrollmentStatus,
    User,
    UserRole,
)

TENANT = uuid.UUID(DEV_TENANT)


def _user(db_session, role=UserRole.student):
    row = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Learner",
        role=role,
        tenant_id=TENANT,
    )
    db_session.add(row)
    db_session.flush()
    return row


@contextlib.contextmanager
def _as(row: User):
    """Act as a real users row: enrolment and audit writes carry its id as a foreign key."""
    who = CurrentUser(
        id=str(row.id),
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        tenant_id=str(row.tenant_id),
        keycloak_id=row.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _course(db_session, *, name="Course", published=True, difficulty="intermediate", tags=(), retired=False):
    row = Course(
        id=uuid.uuid4(),
        name=f"{name} {uuid.uuid4().hex[:6]}",
        is_published=published,
        difficulty=difficulty,
        tags=json.dumps(list(tags)),
        course_meta=json.dumps({"retired": True}) if retired else "{}",
        tenant_id=TENANT,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _enrollment(
    db_session,
    user,
    course,
    *,
    status=EnrollmentStatus.enrolled,
    started_at=None,
    completed_at=None,
    final_grade=None,
    source="self",
):
    row = Enrollment(
        user_id=user.id,
        course_id=course.id,
        tenant_id=TENANT,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        final_grade=final_grade,
        source=source,
    )
    db_session.add(row)
    db_session.flush()
    return row


# ── Import guards ────────────────────────────────────────────────────────────


def test_the_course_content_import_states_its_reasons(client):
    from app.routers.courses import MAX_CSV_BYTES

    too_big = client.post(
        "/courses/import-course-content", files={"file": ("big.yaml", b"a" * (MAX_CSV_BYTES + 1), "application/x-yaml")}
    )
    assert too_big.status_code == 413
    assert "course file too large" in too_big.text

    not_utf8 = client.post(
        "/courses/import-course-content", files={"file": ("bad.yaml", b"\xff\xfe\x00bad", "application/x-yaml")}
    )
    assert not_utf8.status_code == 422
    assert "course file must be UTF-8 YAML" in not_utf8.text


def test_the_programme_import_states_its_reasons(client):
    from app.routers.courses import MAX_CSV_BYTES

    too_big = client.post(
        "/courses/import-programme", files={"file": ("big.csv", b"a" * (MAX_CSV_BYTES + 1), "text/csv")}
    )
    assert too_big.status_code == 413
    assert "programme catalogue too large" in too_big.text

    not_utf8 = client.post("/courses/import-programme", files={"file": ("bad.csv", b"\xff\xfe\x00bad", "text/csv")})
    assert not_utf8.status_code == 422
    assert "programme catalogue must be UTF-8 CSV" in not_utf8.text


# ── Enrolment ────────────────────────────────────────────────────────────────


def test_enrolling_twice_answers_the_same_row(db_session, client):
    student = _user(db_session)
    course = _course(db_session)
    with _as(student) as who:
        first = client.post(f"/courses/{course.id}/enroll", json={"user_id": str(who.id), "course_id": str(course.id)})
        second = client.post(f"/courses/{course.id}/enroll", json={"user_id": str(who.id), "course_id": str(course.id)})
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert db_session.query(Enrollment).filter_by(user_id=student.id, course_id=course.id).count() == 1
    assert db_session.query(Enrollment).filter_by(user_id=student.id).one().source == "self"


def test_staff_enrolling_someone_upgrades_their_self_row(db_session, client):
    student = _user(db_session)
    admin = _user(db_session, UserRole.admin)
    course = _course(db_session)
    with _as(student) as who:
        client.post(f"/courses/{course.id}/enroll", json={"user_id": str(who.id), "course_id": str(course.id)})
    with _as(admin):
        again = client.post(
            f"/courses/{course.id}/enroll", json={"user_id": str(student.id), "course_id": str(course.id)}
        )
    assert again.status_code == 200, again.text
    row = db_session.query(Enrollment).filter_by(user_id=student.id, course_id=course.id).one()
    assert row.source == "assigned"
    detail = db_session.query(AuditLog).filter_by(action="enrollment_update").one().detail
    assert "now assigned" in detail


def test_staff_reopening_a_withdrawn_student_picks_the_status_from_their_start(db_session, client):
    admin = _user(db_session, UserRole.admin)
    course = _course(db_session)
    fresh = _user(db_session)
    started = _user(db_session)
    _enrollment(db_session, fresh, course, status=EnrollmentStatus.withdrawn)
    _enrollment(
        db_session,
        started,
        course,
        status=EnrollmentStatus.withdrawn,
        started_at=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
    )
    with _as(admin):
        for learner in (fresh, started):
            r = client.post(
                f"/courses/{course.id}/enroll", json={"user_id": str(learner.id), "course_id": str(course.id)}
            )
            assert r.status_code == 200, r.text
    statuses = {e.user_id: e.status for e in db_session.query(Enrollment).filter_by(course_id=course.id)}
    assert statuses[fresh.id] == EnrollmentStatus.enrolled
    assert statuses[started.id] == EnrollmentStatus.in_progress
    details = [a.detail for a in db_session.query(AuditLog).filter_by(action="enrollment_update")]
    assert any("withdrawn -> enrolled" in d for d in details)
    assert any("withdrawn -> in_progress" in d for d in details)


def test_a_students_own_withdrawn_enrolment_stays_withdrawn(db_session, client):
    student = _user(db_session)
    course = _course(db_session)
    _enrollment(db_session, student, course, status=EnrollmentStatus.withdrawn)
    with _as(student) as who:
        r = client.post(f"/courses/{course.id}/enroll", json={"user_id": str(who.id), "course_id": str(course.id)})
    assert r.status_code == 200, r.text
    assert db_session.query(Enrollment).filter_by(user_id=student.id).one().status == EnrollmentStatus.withdrawn


def test_an_outcome_is_never_reset_by_re_enrolment(db_session, client):
    admin = _user(db_session, UserRole.admin)
    course = _course(db_session)
    failed = _user(db_session)
    finished = _user(db_session)
    _enrollment(db_session, failed, course, status=EnrollmentStatus.failed, final_grade="F")
    _enrollment(
        db_session,
        finished,
        course,
        status=EnrollmentStatus.completed,
        completed_at=datetime(2026, 1, 2, 9, 0, 0, tzinfo=UTC),
        final_grade="A",
    )
    with _as(admin):
        for learner in (failed, finished):
            r = client.post(
                f"/courses/{course.id}/enroll", json={"user_id": str(learner.id), "course_id": str(course.id)}
            )
            assert r.status_code == 200, r.text
    statuses = {e.user_id: e.status for e in db_session.query(Enrollment).filter_by(course_id=course.id)}
    assert statuses[failed.id] == EnrollmentStatus.failed
    assert statuses[finished.id] == EnrollmentStatus.completed


def test_a_retired_course_takes_no_new_enrolments(db_session, client):
    student = _user(db_session)
    stub = _course(db_session, retired=True)
    with _as(student) as who:
        r = client.post(f"/courses/{stub.id}/enroll", json={"user_id": str(who.id), "course_id": str(stub.id)})
    assert r.status_code == 409
    assert "retired" in r.text


# ── Withdrawal ───────────────────────────────────────────────────────────────


def test_withdraw_records_the_previous_status_and_repeats_cleanly(db_session, client):
    admin = _user(db_session, UserRole.admin)
    course = _course(db_session)
    student = _user(db_session)
    _enrollment(db_session, student, course, status=EnrollmentStatus.in_progress)
    with _as(admin):
        first = client.post(f"/courses/{course.id}/withdraw/{student.id}")
        second = client.post(f"/courses/{course.id}/withdraw/{student.id}")
    assert first.status_code == 200 and second.status_code == 200
    assert db_session.query(Enrollment).filter_by(user_id=student.id).one().status == EnrollmentStatus.withdrawn
    audits = db_session.query(AuditLog).filter_by(action="enrollment_withdraw").all()
    assert len(audits) == 1  # the repeat answers the row unchanged and says nothing
    assert "in_progress -> withdrawn" in audits[0].detail


def test_withdraw_refuses_an_outcome_and_a_missing_enrolment(db_session, client):
    admin = _user(db_session, UserRole.admin)
    course = _course(db_session)
    finished = _user(db_session)
    stranger = _user(db_session)
    _enrollment(
        db_session,
        finished,
        course,
        status=EnrollmentStatus.completed,
        completed_at=datetime(2026, 1, 2, 9, 0, 0, tzinfo=UTC),
        final_grade="A",
    )
    with _as(admin):
        outcome = client.post(f"/courses/{course.id}/withdraw/{finished.id}")
        assert outcome.status_code == 409
        assert "outcome" in outcome.text
        missing = client.post(f"/courses/{course.id}/withdraw/{stranger.id}")
        assert missing.status_code == 404
        assert "Enrollment not found" in missing.text


# ── Catalogue filters ────────────────────────────────────────────────────────


def test_the_tag_filter_ands_and_hides_retired_stubs(db_session, client):
    both = _course(db_session, tags=["DP1", "delivers:none"])
    _course(db_session, tags=["DP1"])
    retired = _course(db_session, tags=["DP1", "delivers:none"], retired=True)
    _course(db_session, tags=["other"])

    with _as(_user(db_session, UserRole.admin)):
        r = client.get("/courses", params={"tags": ["DP1", "delivers:none"]})
        names = [c["name"] for c in r.json()["items"]]
        assert r.json()["total"] == 1 and both.name in names

        r = client.get("/courses", params={"tags": ["DP1", "delivers:none"], "include_retired": "true"})
        names = [c["name"] for c in r.json()["items"]]
        assert {both.name, retired.name} <= set(names)

        r = client.get("/courses", params={"tags": ["DP1", "delivers:none"], "include_retired": "false"})
        assert retired.name not in [c["name"] for c in r.json()["items"]]


def test_the_difficulty_filter_narrows_the_catalogue(db_session, client):
    beginner = _course(db_session, difficulty="beginner")
    _course(db_session, difficulty="advanced")
    with _as(_user(db_session, UserRole.admin)):
        r = client.get("/courses", params={"difficulty": "beginner"})
    assert [c["name"] for c in r.json()["items"]] == [beginner.name]


def test_a_draft_course_is_listed_only_to_its_authors_and_hidden_from_students(db_session, client):
    draft = _course(db_session, published=False)
    with _as(_user(db_session, UserRole.admin)):
        assert draft.name in [c["name"] for c in client.get("/courses").json()["items"]]
    with _as(_user(db_session)):
        r = client.get("/courses")
        assert draft.name not in [c["name"] for c in r.json()["items"]]
        assert client.get(f"/courses/{draft.id}").status_code == 404
