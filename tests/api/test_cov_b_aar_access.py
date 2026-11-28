# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Whose after-action report a Student may read (app/aar_access.py).

``aar:read`` is held by Students too, so tenancy alone is not enough: a Student reads an
AAR only when the exercise is completed (nothing left in it is an answer key, ADR 0005 §5)
**and** they took part in this run — by the roster (an assigned enrolment in a course booked
over the run, seated by the run's end), by a team membership, or by a detection submitted in
the run. Anything else is a 404 (may_read_aar False), the same answer as for another
tenant's exercise. Staff and observers keep the tenant-wide view.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import real_exercise, real_tenant, real_user
from app.aar_access import may_read_aar, participants, scoped_to_participation, took_part
from app.models import (
    Course,
    Enrollment,
    EnrollmentStatus,
    ExerciseState,
    Team,
    TeamMembership,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent

STARTED = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
ENDED = STARTED + timedelta(hours=2)


@pytest.fixture
def world(db_session):
    """A completed exercise that ran STARTED..ENDED, its tenant and its people."""
    a = real_tenant(db_session, "aar-b")
    ex = real_exercise(
        db_session, a.id, "id: s\n", state=ExerciseState.completed, started_at=STARTED, completed_at=ENDED
    )
    return {
        "db": db_session,
        "ex": ex,
        "student": real_user(db_session, UserRole.student, a.id),
        "classmate": real_user(db_session, UserRole.student, a.id),
        "instructor": real_user(db_session, UserRole.instructor, a.id),
        "observer": real_user(db_session, UserRole.observer, a.id),
    }


def _enrol(
    db,
    world,
    who,
    *,
    source="assigned",
    status=EnrollmentStatus.enrolled,
    course=None,
    seat=STARTED - timedelta(minutes=20),
):
    """Enrol ``who`` in a course booked over the run. The seat began at ``seat``: the later
    of ``created_at`` (server default: now) and ``enrolled_at`` is what ``seated_by`` reads,
    so ``created_at`` must be pinned for a run that is already over."""
    course = course or Course(id=uuid.uuid4(), name="Class", tenant_id=world["ex"].tenant_id)
    db.add(course)
    db.flush()
    db.add(
        ScheduledEvent(
            name="lesson",
            tenant_id=world["ex"].tenant_id,
            course_id=course.id,
            state=EventState.active,
            start_time=STARTED - timedelta(minutes=30),
            end_time=ENDED + timedelta(minutes=30),
            exercise_id=world["ex"].id,
        )
    )
    db.add(
        Enrollment(
            user_id=uuid.UUID(world[who].id),
            course_id=course.id,
            tenant_id=world["ex"].tenant_id,
            status=status,
            source=source,
            enrolled_at=seat,
            created_at=seat,
        )
    )
    db.flush()
    return course


def _on_team(db, world, who):
    team = Team(
        id=uuid.uuid4(),
        name=f"team-{uuid.uuid4().hex[:6]}",
        tenant_id=world["ex"].tenant_id,
        exercise_id=world["ex"].id,
    )
    db.add(team)
    db.flush()
    db.add(TeamMembership(id=uuid.uuid4(), team_id=team.id, user_id=uuid.UUID(world[who].id), role="member"))
    db.flush()


def _submitted(db, world, who, *, window_start=STARTED + timedelta(minutes=5)):
    from app.detections.models import DetectionSubmission

    db.add(
        DetectionSubmission(
            id=uuid.uuid4(),
            tenant_id=world["ex"].tenant_id,
            exercise_id=world["ex"].id,
            objective_ref="detect_c2",
            user_id=uuid.UUID(world[who].id),
            query="a:b",
            verdict="recorded",
            window_start=window_start,
        )
    )
    db.flush()


# ── who the scoping applies to ─────────────────────────────────────────
def test_students_are_scoped_every_other_role_is_not(world):
    assert scoped_to_participation(world["student"]) is True
    for staff in (world["instructor"], world["observer"]):
        assert scoped_to_participation(staff) is False


def test_staff_read_every_report_of_their_tenant_even_a_running_one(world):
    world["db"].expire_all()
    world["ex"].state = ExerciseState.running
    assert may_read_aar(world["db"], world["ex"], world["instructor"]) is True


# ── completed, and took part ───────────────────────────────────────────
def test_a_rostered_student_reads_the_report(world):
    _enrol(world["db"], world, "student")
    assert took_part(world["db"], world["ex"], uuid.UUID(world["student"].id)) is True
    assert may_read_aar(world["db"], world["ex"], world["student"]) is True


def test_a_student_on_a_team_reads_the_report(world):
    _on_team(world["db"], world, "student")
    assert may_read_aar(world["db"], world["ex"], world["student"]) is True


def test_a_student_who_submitted_a_detection_in_the_run_reads_the_report(world):
    _submitted(world["db"], world, "student")
    assert may_read_aar(world["db"], world["ex"], world["student"]) is True


def test_a_submission_from_before_the_run_is_not_participation(world):
    _submitted(world["db"], world, "student", window_start=STARTED - timedelta(minutes=1))
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


def test_no_submission_belongs_to_a_run_that_never_started(world):
    world["ex"].started_at = None
    _submitted(world["db"], world, "student")
    assert took_part(world["db"], world["ex"], uuid.UUID(world["student"].id)) is False


# ── completed, but not part of this run ────────────────────────────────
def test_a_student_who_never_took_part_is_404_even_in_the_tenant(world):
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


def test_a_self_enrolment_is_no_seat(world):
    _enrol(world["db"], world, "student", source="self")
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


def test_a_withdrawn_enrolment_is_no_seat(world):
    _enrol(world["db"], world, "student", status=EnrollmentStatus.withdrawn)
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


def test_an_enrolment_seated_after_the_run_is_no_seat(world):
    """A later cohort on the same course must not read an earlier run's answer key."""
    course = _enrol(world["db"], world, "student")
    e = world["db"].query(Enrollment).filter(Enrollment.course_id == course.id).one()
    e.enrolled_at = ENDED + timedelta(days=1)
    world["db"].flush()
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


def test_a_running_exercise_is_the_answer_key_so_no_student_reads_it(world):
    _enrol(world["db"], world, "student")
    _on_team(world["db"], world, "student")
    world["ex"].state = ExerciseState.running
    assert may_read_aar(world["db"], world["ex"], world["student"]) is False


# ── the live class roster ──────────────────────────────────────────────
def test_the_live_class_counts_only_enrolments_still_being_studied(world):
    _enrol(world["db"], world, "student")
    _enrol(world["db"], world, "classmate", status=EnrollmentStatus.completed)
    live = participants(world["db"], world["ex"], now=ENDED)
    assert uuid.UUID(world["student"].id) in live
    assert uuid.UUID(world["classmate"].id) not in live
