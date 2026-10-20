"""The live class roster in the Ops Center (issue #199): ``GET /ops/exercises/{id}/class``.

Who is on it is the AAR's participant rule, kept to enrolments still being studied (an
assigned enrolment in a course booked over the run, a member of one of the exercise's teams, a
detection in this run) plus anyone with a lab on the exercise's own range. A Student's lab is
the one launched from a release of a booked course during the run (each lab has its own range). Per-Student credit comes from their own attempts (ADR 0005).
Staff and observers read it (Ops Center: exercise:read + telemetry:read); Students get 403;
another tenant's exercise is 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.detections.models import DetectionSubmission
from app.lab_sessions.models import LabSession
from app.models import (
    Course,
    Enrollment,
    EnrollmentStatus,
    ExerciseState,
    Objective,
    ObjectiveType,
    Team,
    TeamMembership,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent


@pytest.fixture
def world(db_session, monkeypatch):
    monkeypatch.delenv("LIVE_CLASS_NOT_SEEN_MINUTES", raising=False)
    a = real_tenant(db_session, "lc-a")
    b = real_tenant(db_session, "lc-b")
    started = datetime.now(UTC) - timedelta(minutes=40)
    ex = real_exercise(db_session, a.id, state=ExerciseState.running, started_at=started, max_score=50)
    ex.name = "VIGILANT MAPLE"
    for ref, pts, kind in (("detect_c2", 40, ObjectiveType.detection), ("write_report", 10, ObjectiveType.deliverable)):
        db_session.add(Objective(exercise_id=ex.id, ref_id=ref, objective_type=kind, description="",
                                 validator="opensearch_query", points=pts))
    db_session.flush()
    w = {"a": a, "b": b, "ex": ex, "started": started,
         "sam": real_user(db_session, UserRole.student, a.id),
         "bo": real_user(db_session, UserRole.student, a.id),
         "outsider": real_user(db_session, UserRole.student, a.id),
         "instructor": real_user(db_session, UserRole.instructor, a.id),
         "admin": real_user(db_session, UserRole.admin, a.id),
         "range_ops": real_user(db_session, UserRole.range_ops, a.id),
         "observer": real_user(db_session, UserRole.observer, a.id),
         "foreign_instructor": real_user(db_session, UserRole.instructor, b.id)}
    # Sam is on the class's course, booked over the run; Bo is on a team of the exercise.
    course = Course(id=uuid.uuid4(), name="Class", tenant_id=a.id)
    db_session.add(course)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add(ScheduledEvent(name="lesson", tenant_id=a.id, course_id=course.id, state=EventState.active,
                                  start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=2),
                                  exercise_id=ex.id))
    db_session.add(Enrollment(user_id=uuid.UUID(w["sam"].id), course_id=course.id, tenant_id=a.id,
                              status=EnrollmentStatus.enrolled, created_at=now - timedelta(days=1)))
    team = Team(id=uuid.uuid4(), name="Blue Cell 1", tenant_id=a.id, exercise_id=ex.id, color_hex="#1E5AA8")
    db_session.add(team)
    db_session.flush()
    db_session.add_all([
        TeamMembership(user_id=uuid.UUID(w["bo"].id), team_id=team.id),
        TeamMembership(user_id=uuid.UUID(w["instructor"].id), team_id=team.id),  # staff: not a Student
    ])
    db_session.flush()
    w["team"] = team
    w["course"] = course
    return w


def _get(client, ex):
    return client.get(f"/ops/exercises/{ex.id}/class")


def _attempt(db, w, who, ref, verdict, minutes_ago, query="url.domain:*x*"):
    db.add(DetectionSubmission(
        tenant_id=w["a"].id, exercise_id=w["ex"].id, objective_ref=ref, user_id=uuid.UUID(w[who].id), query=query,
        verdict=verdict, submitted_at=datetime.now(UTC) - timedelta(minutes=minutes_ago), events_matched=4,
        on_target=1 if verdict == "missed" else 4, window_start=w["started"],
    ))
    db.flush()


def _by_id(body, who) -> dict:
    return next(s for s in body["students"] if s["user_id"] == who.id)


# ── who may read it ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["instructor", "admin", "range_ops", "observer"])
def test_staff_and_observers_read_the_class(client, world, role):
    act_as(world[role])
    r = _get(client, world["ex"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exercise_name"] == "VIGILANT MAPLE"
    assert body["state"] == "running"


@pytest.mark.parametrize("who", ["sam", "outsider"])
def test_students_are_refused_even_their_own_class(client, world, who):
    act_as(world[who])
    r = _get(client, world["ex"])
    assert r.status_code == 403, r.text


def test_another_tenants_exercise_is_not_found(client, world):
    act_as(world["foreign_instructor"])
    assert _get(client, world["ex"]).status_code == 404


# ── who is on it ──────────────────────────────────────────────────────────────────────────


def test_roster_is_the_booked_course_and_the_teams_students_only(client, world):
    act_as(world["instructor"])
    body = _get(client, world["ex"]).json()
    ids = {s["user_id"] for s in body["students"]}
    assert ids == {world["sam"].id, world["bo"].id}  # not the outsider, not staff on a team
    bo = _by_id(body, world["bo"])
    assert bo["team"]["name"] == "Blue Cell 1" and bo["team"]["color_hex"] == "#1E5AA8"
    assert _by_id(body, world["sam"])["team"] is None
    assert [o["ref_id"] for o in body["objectives"]] == ["detect_c2", "write_report"]
    assert body["max_score"] == 50


def test_a_submitter_in_this_run_is_on_it_and_a_past_runs_is_not(client, db_session, world):
    _attempt(db_session, world, "outsider", "detect_c2", "missed", 5)
    act_as(world["instructor"])
    assert world["outsider"].id in {s["user_id"] for s in _get(client, world["ex"]).json()["students"]}

    other = real_user(db_session, UserRole.student, world["a"].id)
    db_session.add(DetectionSubmission(
        tenant_id=world["a"].id, exercise_id=world["ex"].id, objective_ref="detect_c2", user_id=uuid.UUID(other.id),
        query="a:b", verdict="achieved", submitted_at=world["started"] - timedelta(days=1),
        window_start=world["started"] - timedelta(days=1),
    ))
    db_session.flush()
    assert other.id not in {s["user_id"] for s in _get(client, world["ex"]).json()["students"]}


# ── credit, attempts and the learning signal ──────────────────────────────────────────────


def test_credit_and_score_come_from_the_students_own_attempts(client, db_session, world):
    _attempt(db_session, world, "sam", "detect_c2", "missed", 20)
    _attempt(db_session, world, "sam", "detect_c2", "achieved", 10)
    act_as(world["instructor"])
    body = _get(client, world["ex"]).json()
    sam, bo = _by_id(body, world["sam"]), _by_id(body, world["bo"])
    assert sam["credited_refs"] == ["detect_c2"] and sam["score"] == 40
    assert (sam["attempts_total"], sam["attempts_credited"]) == (2, 1)
    assert sam["current_objective"] == "write_report"
    assert {o["ref_id"]: o["state"] for o in sam["objectives"]} == {"detect_c2": "credited",
                                                                    "write_report": "not_attempted"}
    assert sam["last_detection_at"] is not None and sam["last_activity_at"] == sam["last_detection_at"]
    assert bo["score"] == 0 and bo["credited_refs"] == []  # the exercise's credit is not Bo's


def test_three_judged_misses_on_one_objective_is_a_learning_signal(client, db_session, world):
    for m in (30, 20, 10):
        _attempt(db_session, world, "bo", "detect_c2", "missed", m)
    _attempt(db_session, world, "sam", "detect_c2", "missed", 12)
    _attempt(db_session, world, "sam", "detect_c2", "missed", 11)
    act_as(world["instructor"])
    body = _get(client, world["ex"]).json()
    bo = _by_id(body, world["bo"])
    learning = [a for a in bo["attention"] if a["kind"] == "learning"]
    assert len(learning) == 1 and learning[0]["objective_ref"] == "detect_c2"
    assert "3 judged misses" in learning[0]["detail"]
    assert not [a for a in _by_id(body, world["sam"])["attention"] if a["kind"] == "learning"]  # 2 is not 3
    assert body["learning_signal_misses"] == 3


def test_credit_after_misses_clears_the_learning_signal(client, db_session, world):
    for m in (30, 20, 10):
        _attempt(db_session, world, "bo", "detect_c2", "missed", m)
    _attempt(db_session, world, "bo", "detect_c2", "achieved", 5)
    act_as(world["instructor"])
    bo = _by_id(_get(client, world["ex"]).json(), world["bo"])
    assert not [a for a in bo["attention"] if a["kind"] == "learning"]


def test_queries_only_for_staff_who_see_the_answer_key(client, db_session, world):
    _attempt(db_session, world, "sam", "detect_c2", "missed", 5, query="process.name:evil.exe")
    act_as(world["instructor"])
    att = _by_id(_get(client, world["ex"]).json(), world["sam"])["attempts"][0]
    assert att["query"] == "process.name:evil.exe" and att["precision"] == 0.25
    for role in ("observer", "range_ops"):
        act_as(world[role])
        att = _by_id(_get(client, world["ex"]).json(), world["sam"])["attempts"][0]
        assert att["query"] is None and att["precision"] is None and att["verdict"] == "missed"


# ── lab faults and not seen recently ──────────────────────────────────────────────────────


def _lab(db, w, who, state, error="", last_seen=None, *, course=None, on_exercise_range=False, age=None,
         activity="mod_001"):
    """A lab session as production makes one: its own range, launched from a release of
    ``course`` (default: the class's booked course). ``age``: created that long ago."""
    from app.models import Range

    tid = w["a"].id
    if course is None:
        course = w["course"]
    sha = uuid.uuid4().hex * 2
    db.add(CourseReleaseBlob(sha256=sha, size=1, data=b"x"))
    db.flush()
    version = 1 + db.query(CourseRelease).filter(CourseRelease.course_id == course.id).count()
    rel = CourseRelease(id=uuid.uuid4(), tenant_id=tid, course_id=course.id, catalogue_code="c", arc2_code="a",
                        run_id="r", slug="s", title="T", version=version, release_digest=sha, learner_digest=sha,
                        platform_digest=sha, instructor_digest=sha, blob_sha256=sha, meta="{}")
    db.add(rel)
    db.flush()
    if on_exercise_range:
        range_id = w["ex"].range_id
    else:
        own = Range(id=uuid.uuid4(), name="lab", template_id=db.get(Range, w["ex"].range_id).template_id, tenant_id=tid)
        db.add(own)
        db.flush()
        range_id = own.id
    when = datetime.now(UTC) - (age or timedelta(minutes=30))
    db.add(LabSession(tenant_id=tid, user_id=uuid.UUID(w[who].id), release_id=rel.id, activity_id=activity,
                      state=state, error=error, profile_id="p", profile_digest="0" * 64, backend="mock",
                      range_id=range_id, last_seen_at=last_seen, created_at=when, updated_at=when))
    db.flush()


def test_a_failed_lab_of_the_booked_course_is_a_lab_fault(client, db_session, world):
    _lab(db_session, world, "sam", "failed", error="clone timed out on esx-03")
    act_as(world["instructor"])
    s = _by_id(_get(client, world["ex"]).json(), world["sam"])
    assert [lab["state"] for lab in s["labs"]] == ["failed"]
    kinds = [a["kind"] for a in s["attention"]]
    assert kinds == ["lab_fault"], kinds  # a lab that is down is not also "not seen"
    assert "clone timed out on esx-03" in s["attention"][0]["detail"]


def test_a_lab_of_another_course_or_before_the_run_is_not_this_class(client, db_session, world):
    other = Course(id=uuid.uuid4(), name="Other", tenant_id=world["a"].id)
    db_session.add(other)
    db_session.flush()
    _lab(db_session, world, "sam", "active", course=other)  # a lab for another course
    _lab(db_session, world, "bo", "destroyed", age=timedelta(days=3))  # ended long before the run
    _lab(db_session, world, "outsider", "active")  # booked course's release, but not on the roster
    act_as(world["instructor"])
    body = _get(client, world["ex"]).json()
    assert _by_id(body, world["sam"])["labs"] == []
    assert _by_id(body, world["bo"])["labs"] == []
    assert world["outsider"].id not in {s["user_id"] for s in body["students"]}


def test_two_overlapping_exercises_on_one_course_list_every_lab_never_one_guessed(client, db_session, world):
    """Two exercises booked to the same course at the same time, and Sam has a live lab for
    one module and a failed lab for another: the class cannot tell which lab is whose
    exercise, so it lists both by session (Reset acts on one session id), and a fault on
    either is a fault."""
    second = real_exercise(db_session, world["a"].id, state=ExerciseState.running, started_at=world["started"])
    now = datetime.now(UTC)
    db_session.add(ScheduledEvent(name="lesson 2", tenant_id=world["a"].id, course_id=world["course"].id,
                                  state=EventState.active, start_time=now - timedelta(hours=1),
                                  end_time=now + timedelta(hours=2), exercise_id=second.id))
    db_session.flush()
    _lab(db_session, world, "sam", "active", activity="mod_003", age=timedelta(minutes=20))
    _lab(db_session, world, "sam", "failed", error="no capacity", activity="mod_004", age=timedelta(minutes=10))
    act_as(world["instructor"])
    for ex in (world["ex"], second):
        sam = _by_id(_get(client, ex).json(), world["sam"])
        assert [(lab["activity_id"], lab["state"]) for lab in sam["labs"]] == [("mod_004", "failed"),
                                                                                ("mod_003", "active")]
        assert len({lab["session_id"] for lab in sam["labs"]}) == 2
        faults = [a for a in sam["attention"] if a["kind"] == "lab_fault"]
        assert len(faults) == 1 and "mod_004" in faults[0]["detail"] and "no capacity" in faults[0]["detail"]


def test_a_lab_on_the_exercises_own_range_puts_its_owner_on_the_roster(client, db_session, world):
    other = Course(id=uuid.uuid4(), name="Other", tenant_id=world["a"].id)
    db_session.add(other)
    db_session.flush()
    _lab(db_session, world, "outsider", "failed", error="no host", course=other, on_exercise_range=True)
    act_as(world["instructor"])
    s = _by_id(_get(client, world["ex"]).json(), world["outsider"])  # may_submit's own-lab relation
    assert [lab["state"] for lab in s["labs"]] == ["failed"] and [a["kind"] for a in s["attention"]] == ["lab_fault"]


def test_an_earlier_cohort_is_not_on_todays_class_but_still_reads_its_aar(client, db_session, world):
    from app.aar_access import took_part

    old = real_user(db_session, UserRole.student, world["a"].id)
    db_session.add(Enrollment(user_id=uuid.UUID(old.id), course_id=world["course"].id, tenant_id=world["a"].id,
                              status=EnrollmentStatus.completed, created_at=datetime.now(UTC) - timedelta(days=400)))
    db_session.flush()
    act_as(world["instructor"])
    ids = {s["user_id"] for s in _get(client, world["ex"]).json()["students"]}
    assert old.id not in ids and world["sam"].id in ids
    # The AAR rule is unchanged: a completed enrolment from before the run still took part.
    assert took_part(db_session, world["ex"], uuid.UUID(old.id)) is True


def test_not_seen_recently_is_neutral_and_only_while_running(client, db_session, world):
    _attempt(db_session, world, "sam", "detect_c2", "missed", 2)
    _lab(db_session, world, "bo", "active", last_seen=datetime.now(UTC) - timedelta(minutes=25))
    act_as(world["instructor"])
    body = _get(client, world["ex"]).json()
    assert not _by_id(body, world["sam"])["attention"]
    quiet = [a for a in _by_id(body, world["bo"])["attention"] if a["kind"] == "not_seen"]
    assert len(quiet) == 1 and quiet[0]["detail"].startswith("No activity seen for 2")
    assert body["not_seen_minutes"] == 15
    for s in body["students"]:
        for a in s["attention"]:
            assert "struggl" not in a["detail"].lower()

    world["ex"].state = ExerciseState.paused
    db_session.flush()
    body = _get(client, world["ex"]).json()
    assert not [a for s in body["students"] for a in s["attention"] if a["kind"] == "not_seen"]
