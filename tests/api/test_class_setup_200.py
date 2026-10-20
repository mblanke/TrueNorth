"""An instructor sets up their own class without an admin (#200).

The rule (app/class_setup/scope.py): you teach a course, or run an exercise, when a live
booking (not draft, not cancelled) in your tenant names you its instructor and that course
or exercise. Then, with ``enrollment:manage_own`` / ``team:manage_own``, you may enrol and
withdraw Students on the course and create and change the exercise's teams. Everything
else is refused: another course or exercise (403), another tenant's anything (404), staff
as targets (403), Students and observers (403). Admins keep ``user:update`` unchanged.
Each change leaves an audit row naming who made it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.enrollment import ASSIGNED, ensure_enrollment
from app.models import (
    AuditLog,
    Course,
    Enrollment,
    EnrollmentStatus,
    ExerciseState,
    Team,
    TeamMembership,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent

NOW = datetime.now(UTC)


def _book(db, tenant_id, instructor, *, course=None, exercise=None, state=EventState.scheduled):
    evt = ScheduledEvent(
        name="class", tenant_id=tenant_id, instructor_id=uuid.UUID(instructor.id), state=state,
        course_id=course.id if course else None, exercise_id=exercise.id if exercise else None,
        range_id=exercise.range_id if exercise else None,
        start_time=NOW + timedelta(days=1), end_time=NOW + timedelta(days=1, hours=4),
    )
    db.add(evt)
    db.flush()
    return evt


def _course(db, tenant_id, name="Course"):
    c = Course(id=uuid.uuid4(), name=name, tenant_id=tenant_id, is_published=True)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def world(db_session):
    """Tenant A: an instructor who teaches course ``mine`` and runs exercise ``ex``, a second
    instructor, a Student, an observer, an admin, and course ``theirs`` (not taught by the
    first instructor). Tenant B: its own course, exercise, Student and team."""
    db = db_session
    a, b = real_tenant(db, "class-a"), real_tenant(db, "class-b")
    w = {
        "a": a, "b": b,
        "instructor": real_user(db, UserRole.instructor, a.id),
        "colleague": real_user(db, UserRole.instructor, a.id),
        "student": real_user(db, UserRole.student, a.id),
        "student2": real_user(db, UserRole.student, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "b_student": real_user(db, UserRole.student, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        "mine": _course(db, a.id, "Mine"),
        "theirs": _course(db, a.id, "Theirs"),
        "b_course": _course(db, b.id, "B course"),
        "ex": real_exercise(db, a.id),
        "other_ex": real_exercise(db, a.id),
        "b_ex": real_exercise(db, b.id),
    }
    _book(db, a.id, w["instructor"], course=w["mine"], exercise=w["ex"])
    _book(db, a.id, w["colleague"], course=w["theirs"], exercise=w["other_ex"])
    _book(db, b.id, w["b_instructor"], course=w["b_course"], exercise=w["b_ex"])
    b_team = Team(id=uuid.uuid4(), name="B team", tenant_id=b.id, exercise_id=w["b_ex"].id)
    db.add(b_team)
    db.flush()
    w["b_team"] = b_team
    return w


def _enrol(client, course, who):
    return client.post(f"/courses/{course.id}/enroll", json={"user_id": who.id, "course_id": str(course.id)})


def _row(db, who, course):
    db.expire_all()
    return db.query(Enrollment).filter(Enrollment.user_id == uuid.UUID(who.id), Enrollment.course_id == course.id).first()


def _audits(db, actor, action):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.user_id == uuid.UUID(actor.id), AuditLog.action == action).all()


# ── Enrolment ────────────────────────────────────────────────────────────────


def test_instructor_enrols_a_student_on_a_course_they_teach_and_it_is_audited(client, db_session, world):
    w = world
    act_as(w["instructor"])
    r = _enrol(client, w["mine"], w["student"])
    assert r.status_code == 201, r.text
    row = _row(db_session, w["student"], w["mine"])
    assert row.status == EnrollmentStatus.enrolled and row.source == ASSIGNED
    [audit] = _audits(db_session, w["instructor"], "enrollment_create")
    assert audit.resource_id == str(row.id) and str(w["student"].id) in audit.detail
    assert audit.tenant_id == w["a"].id


def test_instructor_enrols_on_a_shared_course_through_their_own_booking(client, db_session, world):
    w = world
    shared = _course(db_session, None, "Shared")
    _book(db_session, w["a"].id, w["instructor"], course=shared)
    act_as(w["instructor"])
    assert _enrol(client, shared, w["student"]).status_code == 201


@pytest.mark.parametrize("state", [EventState.draft, EventState.cancelled])
def test_a_draft_or_cancelled_booking_does_not_make_you_its_teacher(client, db_session, world, state):
    w = world
    course = _course(db_session, w["a"].id, "Not live")
    _book(db_session, w["a"].id, w["instructor"], course=course, state=state)
    act_as(w["instructor"])
    assert _enrol(client, course, w["student"]).status_code == 403
    assert _row(db_session, w["student"], course) is None


def test_a_completed_or_past_booking_grants_nothing(client, db_session, world):
    """Rule (a): only an upcoming or running booking of your own gives scope."""
    w = world
    course = _course(db_session, w["a"].id, "Taught last week")
    _book(db_session, w["a"].id, w["instructor"], course=course, state=EventState.completed)
    ended = _book(db_session, w["a"].id, w["instructor"], course=course)  # scheduled, but already over
    ended.start_time, ended.end_time = NOW - timedelta(days=8), NOW - timedelta(days=7)
    db_session.flush()
    act_as(w["instructor"])
    assert _enrol(client, course, w["student"]).status_code == 403
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=course.id, tenant_id=w["a"].id)
    assert client.post(f"/courses/{course.id}/withdraw/{w['student'].id}").status_code == 403
    assert str(course.id) not in {c["id"] for c in client.get("/class-setup/mine").json()["courses"]}


def test_instructor_cannot_enrol_on_a_course_someone_else_teaches(client, db_session, world):
    w = world
    act_as(w["instructor"])
    r = _enrol(client, w["theirs"], w["student"])
    assert r.status_code == 403
    assert _row(db_session, w["student"], w["theirs"]) is None
    assert _audits(db_session, w["instructor"], "enrollment_create") == []


def test_a_booking_in_another_tenant_naming_you_gives_nothing(client, db_session, world):
    """Defence in depth: only bookings of the caller's own tenant count."""
    w = world
    shared = _course(db_session, None, "Shared B")
    _book(db_session, w["b"].id, w["instructor"], course=shared)
    act_as(w["instructor"])
    assert _enrol(client, shared, w["student"]).status_code == 403


def test_another_tenants_course_or_student_is_404(client, db_session, world):
    w = world
    act_as(w["instructor"])
    assert _enrol(client, w["b_course"], w["student"]).status_code == 404
    assert _enrol(client, w["mine"], w["b_student"]).status_code == 404
    assert _row(db_session, w["b_student"], w["mine"]) is None


@pytest.mark.parametrize("target", ["colleague", "observer", "admin"])
def test_instructor_enrols_only_students(client, db_session, world, target):
    w = world
    act_as(w["instructor"])
    assert _enrol(client, w["mine"], w[target]).status_code == 403
    assert _row(db_session, w[target], w["mine"]) is None


@pytest.mark.parametrize("caller", ["student", "observer"])
def test_students_and_observers_cannot_enrol_anyone_else(client, db_session, world, caller):
    w = world
    act_as(w[caller])
    assert _enrol(client, w["mine"], w["student2"]).status_code == 403
    assert _row(db_session, w["student2"], w["mine"]) is None


def test_a_student_still_enrols_themselves(client, db_session, world):
    w = world
    act_as(w["student"])
    assert _enrol(client, w["mine"], w["student"]).status_code == 201


def test_admin_unchanged_enrols_anyone_in_the_tenant_without_a_booking(client, db_session, world):
    w = world
    act_as(w["admin"])
    assert _enrol(client, w["theirs"], w["colleague"]).status_code == 201
    assert _audits(db_session, w["admin"], "enrollment_create")
    assert _enrol(client, w["b_course"], w["student"]).status_code == 404
    assert _enrol(client, w["mine"], w["b_student"]).status_code == 404


# ── Withdrawal ───────────────────────────────────────────────────────────────


def _withdraw(client, course, who):
    return client.post(f"/courses/{course.id}/withdraw/{who.id}")


def test_instructor_withdraws_and_re_enrols_a_student_audited(client, db_session, world):
    w = world
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=w["mine"].id, tenant_id=w["a"].id)
    act_as(w["instructor"])

    r = _withdraw(client, w["mine"], w["student"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn"
    assert _row(db_session, w["student"], w["mine"]).status == EnrollmentStatus.withdrawn
    [audit] = _audits(db_session, w["instructor"], "enrollment_withdraw")
    assert "enrolled -> withdrawn" in audit.detail

    # Again: unchanged, no second audit row.
    assert _withdraw(client, w["mine"], w["student"]).status_code == 200
    assert len(_audits(db_session, w["instructor"], "enrollment_withdraw")) == 1

    # The Student cannot reopen it themselves...
    act_as(w["student"])
    assert _enrol(client, w["mine"], w["student"]).json()["status"] == "withdrawn"
    # ...their instructor can.
    act_as(w["instructor"])
    r = _enrol(client, w["mine"], w["student"])
    assert r.status_code == 200 and r.json()["status"] == "enrolled"
    assert _audits(db_session, w["instructor"], "enrollment_update")


def test_withdraw_refusals(client, db_session, world):
    w = world
    for who, course in ((w["student"], w["theirs"]), (w["student"], w["mine"])):
        ensure_enrollment(db_session, user_id=uuid.UUID(who.id), course_id=course.id, tenant_id=w["a"].id)
    act_as(w["instructor"])
    assert _withdraw(client, w["theirs"], w["student"]).status_code == 403  # not their course
    assert _withdraw(client, w["b_course"], w["student"]).status_code == 404  # other tenant's course
    assert _withdraw(client, w["mine"], w["b_student"]).status_code == 404  # other tenant's Student
    assert _withdraw(client, w["mine"], w["student2"]).status_code == 404  # not enrolled
    assert _withdraw(client, w["mine"], w["colleague"]).status_code == 403  # staff
    assert _row(db_session, w["student"], w["theirs"]).status == EnrollmentStatus.enrolled

    row = _row(db_session, w["student"], w["mine"])
    row.status = EnrollmentStatus.completed
    db_session.flush()
    assert _withdraw(client, w["mine"], w["student"]).status_code == 409

    for caller in ("student", "observer"):
        act_as(w[caller])
        assert _withdraw(client, w["mine"], w["student2"]).status_code == 403


def test_admin_withdraws_anyone_in_the_tenant(client, db_session, world):
    w = world
    ensure_enrollment(db_session, user_id=uuid.UUID(w["colleague"].id), course_id=w["theirs"].id, tenant_id=w["a"].id)
    act_as(w["admin"])
    assert _withdraw(client, w["theirs"], w["colleague"]).status_code == 200
    assert _audits(db_session, w["admin"], "enrollment_withdraw")


# ── What is mine ─────────────────────────────────────────────────────────────


def test_class_setup_mine_lists_only_the_callers_live_classes(client, db_session, world):
    w = world
    _book(db_session, w["a"].id, w["instructor"], course=w["theirs"], state=EventState.cancelled)
    act_as(w["instructor"])
    r = client.get("/class-setup/mine")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body["courses"]] == [str(w["mine"].id)]
    assert [e["id"] for e in body["exercises"]] == [str(w["ex"].id)]
    for caller in ("student", "observer"):
        act_as(w[caller])
        assert client.get("/class-setup/mine").status_code == 403


# ── Teams ────────────────────────────────────────────────────────────────────


def _team(client, exercise=None, name="Blue"):
    body = {"name": name, "team_type": "blue"}
    if exercise is not None:
        body["exercise_id"] = str(exercise.id)
    return client.post("/teams", json=body)


def _team_audits(db, actor, action, team_id):
    return [a for a in _audits(db, actor, action) if a.resource_type == "team" and a.resource_id == str(team_id)]


def test_instructor_sets_up_the_teams_of_an_exercise_they_run(client, db_session, world):
    w = world
    # The Student is in the class: assigned to the course of the instructor's booking of ex.
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=w["mine"].id, tenant_id=w["a"].id)
    act_as(w["instructor"])
    r = _team(client, w["ex"])
    assert r.status_code == 201, r.text
    team_id = r.json()["id"]
    assert r.json()["exercise_id"] == str(w["ex"].id)
    assert _team_audits(db_session, w["instructor"], "create", team_id)

    r = client.patch(f"/teams/{team_id}", json={"name": "Blue cell", "max_members": 4})
    assert r.status_code == 200 and r.json()["name"] == "Blue cell"
    [upd] = _team_audits(db_session, w["instructor"], "update", team_id)
    assert upd.detail == "max_members, name"

    r = client.post(f"/teams/{team_id}/members", params={"user_id": w["student"].id})
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.query(TeamMembership).filter(TeamMembership.team_id == uuid.UUID(team_id)).count() == 1
    assert _team_audits(db_session, w["instructor"], "add_member", team_id)

    assert client.delete(f"/teams/{team_id}/members/{w['student'].id}").status_code == 204
    assert _team_audits(db_session, w["instructor"], "remove_member", team_id)

    assert client.delete(f"/teams/{team_id}").status_code == 204
    assert _team_audits(db_session, w["instructor"], "delete", team_id)


def test_instructor_team_refusals(client, db_session, world):
    w = world
    act_as(w["instructor"])
    assert _team(client).status_code == 403  # a standing team is an admin's
    assert _team(client, w["other_ex"]).status_code == 403  # someone else's exercise
    assert _team(client, w["b_ex"]).status_code == 404  # another tenant's exercise
    db_session.expire_all()
    assert db_session.query(Team).filter(Team.tenant_id == w["a"].id).count() == 0

    standing = Team(id=uuid.uuid4(), name="Standing", tenant_id=w["a"].id)
    theirs = Team(id=uuid.uuid4(), name="Theirs", tenant_id=w["a"].id, exercise_id=w["other_ex"].id)
    db_session.add_all([standing, theirs])
    db_session.flush()
    for t in (standing, theirs):
        assert client.patch(f"/teams/{t.id}", json={"name": "x"}).status_code == 403
        assert client.post(f"/teams/{t.id}/members", params={"user_id": w["student"].id}).status_code == 403
        assert client.delete(f"/teams/{t.id}").status_code == 403
    b = w["b_team"]
    assert client.patch(f"/teams/{b.id}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/teams/{b.id}").status_code == 404

    mine = _team(client, w["ex"]).json()["id"]
    assert client.post(f"/teams/{mine}/members", params={"user_id": w["colleague"].id}).status_code == 403  # staff
    assert client.post(f"/teams/{mine}/members", params={"user_id": w["b_student"].id}).status_code == 404
    # A Student of the tenant who is not in the class (not assigned to the session's course).
    r = client.post(f"/teams/{mine}/members", params={"user_id": w["student2"].id})
    assert r.status_code == 403 and "enrolled in your session's course" in r.json()["detail"]
    # Self-enrolment is no seat either.
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student2"].id), course_id=w["mine"].id,
                      tenant_id=w["a"].id, source="self")
    assert client.post(f"/teams/{mine}/members", params={"user_id": w["student2"].id}).status_code == 403


def test_instructor_cannot_remove_staff_an_admin_placed_nor_delete_their_team(client, db_session, world):
    w = world
    team = Team(id=uuid.uuid4(), name="Mine", tenant_id=w["a"].id, exercise_id=w["ex"].id)
    db_session.add(team)
    db_session.flush()
    db_session.add(TeamMembership(team_id=team.id, user_id=uuid.UUID(w["colleague"].id)))
    db_session.flush()
    act_as(w["instructor"])
    assert client.delete(f"/teams/{team.id}/members/{w['colleague'].id}").status_code == 403
    assert client.delete(f"/teams/{team.id}").status_code == 403
    db_session.expire_all()
    assert db_session.query(TeamMembership).filter(TeamMembership.team_id == team.id).count() == 1
    assert _team_audits(db_session, w["instructor"], "delete", team.id) == []
    # An admin deletes it; the audit row names the members removed.
    act_as(w["admin"])
    assert client.delete(f"/teams/{team.id}").status_code == 204
    [row] = _team_audits(db_session, w["admin"], "delete", team.id)
    assert w["colleague"].id in row.detail


def test_instructor_deletes_a_students_only_team_audited_with_its_members(client, db_session, world):
    w = world
    team = Team(id=uuid.uuid4(), name="Mine", tenant_id=w["a"].id, exercise_id=w["ex"].id)
    db_session.add(team)
    db_session.flush()
    db_session.add(TeamMembership(team_id=team.id, user_id=uuid.UUID(w["student"].id)))
    db_session.flush()
    act_as(w["instructor"])
    assert client.delete(f"/teams/{team.id}").status_code == 204
    [row] = _team_audits(db_session, w["instructor"], "delete", team.id)
    assert w["student"].id in row.detail


@pytest.mark.parametrize("caller", ["student", "observer"])
def test_students_and_observers_cannot_change_teams(client, db_session, world, caller):
    w = world
    team = Team(id=uuid.uuid4(), name="Mine", tenant_id=w["a"].id, exercise_id=w["ex"].id)
    db_session.add(team)
    db_session.flush()
    act_as(w[caller])
    assert _team(client, w["ex"]).status_code == 403
    assert client.patch(f"/teams/{team.id}", json={"name": "x"}).status_code == 403
    assert client.post(f"/teams/{team.id}/members", params={"user_id": w["student"].id}).status_code == 403
    assert client.delete(f"/teams/{team.id}").status_code == 403


def test_admin_teams_unchanged(client, db_session, world):
    w = world
    act_as(w["admin"])
    r = _team(client)
    assert r.status_code == 201 and r.json()["exercise_id"] is None
    tid = r.json()["id"]
    assert client.post(f"/teams/{tid}/members", params={"user_id": w["colleague"].id}).status_code == 201
    assert _team(client, w["other_ex"]).status_code == 201  # any exercise of the tenant
    assert _team(client, w["b_ex"]).status_code == 404


# ── Rule (a): your own upcoming or running session; never into another's running one ──


def _running(db, tenant_id, instructor, **kw):
    evt = _book(db, tenant_id, instructor, state=EventState.active, **kw)
    evt.start_time, evt.end_time = NOW - timedelta(hours=1), NOW + timedelta(hours=3)
    db.flush()
    return evt


def test_two_instructors_same_course_past_one_has_no_scope_current_one_does(client, db_session, world):
    w = world
    course = _course(db_session, w["a"].id, "Shared class")
    past = _book(db_session, w["a"].id, w["colleague"], course=course, state=EventState.completed)
    past.start_time, past.end_time = NOW - timedelta(days=30), NOW - timedelta(days=29)
    _book(db_session, w["a"].id, w["instructor"], course=course)  # upcoming
    db_session.flush()
    act_as(w["colleague"])
    assert _enrol(client, course, w["student"]).status_code == 403
    act_as(w["instructor"])
    assert _enrol(client, course, w["student"]).status_code == 201


def test_a_colleagues_running_session_is_409_for_enrolment_and_teams(client, db_session, world):
    w = world
    course = _course(db_session, w["a"].id, "Live class")
    ex = real_exercise(db_session, w["a"].id)
    _running(db_session, w["a"].id, w["colleague"], course=course, exercise=ex)
    _book(db_session, w["a"].id, w["instructor"], course=course, exercise=ex)  # own, upcoming
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=course.id, tenant_id=w["a"].id)
    team = Team(id=uuid.uuid4(), name="Their live team", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(team)
    db_session.flush()
    act_as(w["instructor"])
    r = _enrol(client, course, w["student2"])
    assert r.status_code == 409 and "is running" in r.json()["detail"]
    assert _withdraw(client, course, w["student"]).status_code == 409
    assert _team(client, ex).status_code == 409
    assert client.patch(f"/teams/{team.id}", json={"name": "x"}).status_code == 409
    assert client.post(f"/teams/{team.id}/members", params={"user_id": w["student"].id}).status_code == 409
    assert client.delete(f"/teams/{team.id}").status_code == 409
    assert _row(db_session, w["student"], course).status == EnrollmentStatus.enrolled
    # The colleague running it is not blocked by their own session; an admin never is.
    act_as(w["colleague"])
    assert _enrol(client, course, w["student2"]).status_code == 201
    act_as(w["admin"])
    assert _withdraw(client, course, w["student2"]).status_code == 200


def test_mallorys_future_zero_resource_booking_cannot_touch_alices_live_run(client, db_session, world):
    """The first review's PoC 1, through the real booking API."""
    w = world
    alice, mallory = w["instructor"], w["colleague"]
    course = _course(db_session, w["a"].id, "Alice's class")
    ex = real_exercise(db_session, w["a"].id, state=ExerciseState.running, started_at=NOW - timedelta(hours=1))
    _running(db_session, w["a"].id, alice, course=course, exercise=ex)
    db_session.add(Enrollment(user_id=uuid.UUID(w["student"].id), course_id=course.id, tenant_id=w["a"].id,
                              source=ASSIGNED, status=EnrollmentStatus.in_progress, started_at=NOW))
    team = Team(id=uuid.uuid4(), name="Alice's team", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(team)
    db_session.flush()

    act_as(mallory)
    year = NOW + timedelta(days=365)
    r = client.post("/schedule/events", json={
        "name": "mine now", "course_id": str(course.id), "exercise_id": str(ex.id),
        "start_time": year.isoformat(), "end_time": (year + timedelta(hours=1)).isoformat()})
    assert r.status_code == 201, r.text  # bookings stay self-service
    assert _withdraw(client, course, w["student"]).status_code == 409
    assert _enrol(client, course, w["student2"]).status_code == 409
    assert client.patch(f"/teams/{team.id}", json={"name": "mine"}).status_code == 409
    assert client.delete(f"/teams/{team.id}").status_code == 409
    assert _row(db_session, w["student"], course).status == EnrollmentStatus.in_progress


# ── Teams can't open a finished run's AAR (ADR 0005 §5) ──────────────────────


def test_team_membership_cannot_grant_a_completed_runs_aar(client, db_session, world):
    """The first review's PoC 2: book a completed exercise, team a Student onto it, read its AAR."""
    from app.aar_access import took_part

    w = world
    ex = real_exercise(db_session, w["a"].id, state=ExerciseState.completed,
                       started_at=NOW - timedelta(days=3), completed_at=NOW - timedelta(days=2))
    old_team = Team(id=uuid.uuid4(), name="Old team", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(old_team)
    db_session.flush()
    outsider = uuid.UUID(w["student2"].id)
    assert not took_part(db_session, ex, outsider)

    act_as(w["colleague"])
    later = NOW + timedelta(days=300)
    r = client.post("/schedule/events", json={
        "name": "peek", "exercise_id": str(ex.id), "course_id": str(w["theirs"].id),
        "start_time": later.isoformat(), "end_time": (later + timedelta(hours=1)).isoformat()})
    assert r.status_code == 201, r.text
    ensure_enrollment(db_session, user_id=outsider, course_id=w["theirs"].id, tenant_id=w["a"].id)
    assert _team(client, ex).status_code == 409
    assert client.post(f"/teams/{old_team.id}/members", params={"user_id": str(outsider)}).status_code == 409
    act_as(w["admin"])  # nobody adds members to a finished run's team
    assert client.post(f"/teams/{old_team.id}/members", params={"user_id": str(outsider)}).status_code == 409
    db_session.expire_all()
    assert not took_part(db_session, ex, outsider)


def test_a_live_exercise_team_takes_only_the_sessions_enrolled_students(client, db_session, world):
    """A booking of the exercise with no course has no class: an instructor teams nobody onto it."""
    from app.aar_access import took_part

    w = world
    ex = real_exercise(db_session, w["a"].id)
    _book(db_session, w["a"].id, w["colleague"], exercise=ex)  # no course
    db_session.flush()
    act_as(w["colleague"])
    tid = _team(client, ex).json()["id"]
    assert client.post(f"/teams/{tid}/members", params={"user_id": w["student"].id}).status_code == 403
    db_session.expire_all()
    assert not took_part(db_session, ex, uuid.UUID(w["student"].id))


# ── Outcomes are not laundered ───────────────────────────────────────────────


def test_failed_is_not_withdrawn_and_reopening_keeps_progress(client, db_session, world):
    w = world
    sid, s2 = uuid.UUID(w["student"].id), uuid.UUID(w["student2"].id)
    db_session.add_all([
        Enrollment(user_id=sid, course_id=w["mine"].id, tenant_id=w["a"].id, source=ASSIGNED,
                   status=EnrollmentStatus.failed, completed_at=NOW, final_grade="F"),
        Enrollment(user_id=s2, course_id=w["mine"].id, tenant_id=w["a"].id, source=ASSIGNED,
                   status=EnrollmentStatus.in_progress, started_at=NOW - timedelta(days=1)),
    ])
    db_session.flush()
    act_as(w["instructor"])
    r = _withdraw(client, w["mine"], w["student"])
    assert r.status_code == 409 and "failed" in r.json()["detail"]
    # Re-enrolling a failed Student does not reset the outcome.
    assert _enrol(client, w["mine"], w["student"]).json()["status"] == "failed"

    assert _withdraw(client, w["mine"], w["student2"]).json()["status"] == "withdrawn"
    assert _enrol(client, w["mine"], w["student2"]).json()["status"] == "in_progress"  # not reset to enrolled


def test_a_withdrawn_enrolment_with_a_grade_is_not_reopened(client, db_session, world):
    w = world
    db_session.add(Enrollment(user_id=uuid.UUID(w["student"].id), course_id=w["mine"].id, tenant_id=w["a"].id,
                              source=ASSIGNED, status=EnrollmentStatus.withdrawn, final_grade="D"))
    db_session.flush()
    act_as(w["admin"])
    assert _enrol(client, w["mine"], w["student"]).json()["status"] == "withdrawn"


# ── Team references stay in the tenant ───────────────────────────────────────


@pytest.mark.parametrize("caller", ["admin", "instructor"])
def test_team_ou_and_nation_must_be_valid_for_the_tenant(client, db_session, world, caller):
    from app.models import Nation, OrganizationalUnit

    w = world
    b_ou = OrganizationalUnit(id=uuid.uuid4(), name="B unit", slug="b-unit", tenant_id=w["b"].id)
    a_ou = OrganizationalUnit(id=uuid.uuid4(), name="A unit", slug="a-unit", tenant_id=w["a"].id)
    nation = Nation(id=uuid.uuid4(), name="Testland", iso_alpha2="TQ", iso_alpha3="TQL")
    db_session.add_all([b_ou, a_ou, nation])
    db_session.flush()
    act_as(w[caller])
    base = {"name": "Refs", "exercise_id": str(w["ex"].id)}
    assert client.post("/teams", json={**base, "ou_id": str(b_ou.id)}).status_code == 422
    assert client.post("/teams", json={**base, "nation_id": str(uuid.uuid4())}).status_code == 422
    r = client.post("/teams", json={**base, "ou_id": str(a_ou.id), "nation_id": str(nation.id)})
    assert r.status_code == 201, r.text


# ── Second review (r2) ───────────────────────────────────────────────────────


def _completed_run_of(db, w, course):
    """A completed run of a fresh exercise, booked for ``course`` (its roster) yesterday."""
    ex = real_exercise(db, w["a"].id, state=ExerciseState.completed,
                       started_at=NOW - timedelta(days=1, hours=4), completed_at=NOW - timedelta(days=1))
    evt = _book(db, w["a"].id, w["colleague"], course=course, exercise=ex, state=EventState.completed)
    evt.start_time, evt.end_time = NOW - timedelta(days=1, hours=5), NOW - timedelta(days=1)
    db.flush()
    return ex


def test_r2_reopening_after_a_run_does_not_seat_the_student_in_it(client, db_session, world):
    """Item 1: enrolled_at is "rostered since"; reopening moves it to now."""
    from app.aar_access import took_part

    w = world
    ex = _completed_run_of(db_session, w, w["mine"])
    before = NOW - timedelta(days=3)
    sid, s2 = uuid.UUID(w["student"].id), uuid.UUID(w["student2"].id)
    db_session.add_all([
        # Withdrawn before the run.
        Enrollment(user_id=sid, course_id=w["mine"].id, tenant_id=w["a"].id, source=ASSIGNED,
                   status=EnrollmentStatus.withdrawn, created_at=before, enrolled_at=before),
        # Seated through the run (the control).
        Enrollment(user_id=s2, course_id=w["mine"].id, tenant_id=w["a"].id, source=ASSIGNED,
                   status=EnrollmentStatus.enrolled, created_at=before, enrolled_at=before),
    ])
    db_session.flush()
    assert took_part(db_session, ex, s2) is True
    assert took_part(db_session, ex, sid) is False

    act_as(w["instructor"])  # holds next week's booking of "mine"
    r = _enrol(client, w["mine"], w["student"])
    assert r.status_code == 200 and r.json()["status"] == "enrolled"
    db_session.expire_all()
    assert took_part(db_session, ex, sid) is False


def test_r2_a_self_enrolment_turned_assigned_after_a_run_is_not_seated_in_it(client, db_session, world):
    from app.aar_access import took_part

    w = world
    ex = _completed_run_of(db_session, w, w["mine"])
    before = NOW - timedelta(days=3)
    sid = uuid.UUID(w["student"].id)
    db_session.add(Enrollment(user_id=sid, course_id=w["mine"].id, tenant_id=w["a"].id, source="self",
                              status=EnrollmentStatus.enrolled, created_at=before, enrolled_at=before))
    db_session.flush()
    act_as(w["instructor"])
    assert _enrol(client, w["mine"], w["student"]).status_code == 200
    db_session.expire_all()
    assert _row(db_session, w["student"], w["mine"]).source == ASSIGNED
    assert took_part(db_session, ex, sid) is False


def test_r2_a_range_linked_running_booking_is_the_same_exercise(client, db_session, world):
    """Item 2: a colleague's running booking that names the exercise's range, not the exercise."""
    w = world
    ex = real_exercise(db_session, w["a"].id)
    evt = _running(db_session, w["a"].id, w["colleague"], exercise=ex)
    evt.exercise_id = None  # booked by range only
    _book(db_session, w["a"].id, w["instructor"], exercise=ex)
    team = Team(id=uuid.uuid4(), name="Live", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(team)
    db_session.flush()
    act_as(w["instructor"])
    assert client.patch(f"/teams/{team.id}", json={"name": "x"}).status_code == 409


def test_r2_a_running_exercise_is_409_unless_the_running_session_is_yours(client, db_session, world):
    """Item 2: started by hand or overrunning its booking, the run itself blocks."""
    w = world
    ex = real_exercise(db_session, w["a"].id, state=ExerciseState.running, started_at=NOW - timedelta(hours=6))
    over = _book(db_session, w["a"].id, w["colleague"], exercise=ex, state=EventState.completed)
    over.start_time, over.end_time = NOW - timedelta(hours=6), NOW - timedelta(hours=1)
    _book(db_session, w["a"].id, w["instructor"], exercise=ex)  # next week
    team = Team(id=uuid.uuid4(), name="Live", tenant_id=w["a"].id, exercise_id=ex.id)
    db_session.add(team)
    db_session.flush()
    act_as(w["instructor"])
    r = client.patch(f"/teams/{team.id}", json={"name": "x"})
    assert r.status_code == 409 and "running outside your own session" in r.json()["detail"]
    assert _team(client, ex).status_code == 409
    # Paused counts too.
    ex.state = ExerciseState.paused
    db_session.flush()
    assert client.delete(f"/teams/{team.id}").status_code == 409

    # The instructor whose own booking is running it now is not blocked by their own run.
    ex.state = ExerciseState.running
    own = real_exercise(db_session, w["a"].id, state=ExerciseState.running, started_at=NOW - timedelta(minutes=30))
    _running(db_session, w["a"].id, w["instructor"], exercise=own)
    mine = Team(id=uuid.uuid4(), name="Mine live", tenant_id=w["a"].id, exercise_id=own.id)
    db_session.add(mine)
    db_session.flush()
    assert client.patch(f"/teams/{mine.id}", json={"name": "renamed"}).status_code == 200


def test_r2_overrunning_run_blocks_enrol_and_withdraw_on_its_course(client, db_session, world):
    """Item 2: the course's booking ended, its exercise still runs and still seats the course."""
    w = world
    course = _course(db_session, w["a"].id, "Overrun")
    ex = real_exercise(db_session, w["a"].id, state=ExerciseState.running, started_at=NOW - timedelta(hours=6))
    over = _book(db_session, w["a"].id, w["colleague"], course=course, exercise=ex, state=EventState.completed)
    over.start_time, over.end_time = NOW - timedelta(hours=6), NOW - timedelta(hours=1)
    _book(db_session, w["a"].id, w["instructor"], course=course)  # next week
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=course.id, tenant_id=w["a"].id)
    db_session.flush()
    act_as(w["instructor"])
    r = _withdraw(client, course, w["student"])
    assert r.status_code == 409 and "is running" in r.json()["detail"]
    assert _enrol(client, course, w["student2"]).status_code == 409
    assert _row(db_session, w["student"], course).status == EnrollmentStatus.enrolled
    # Once the run ends, next week's instructor may change the class.
    ex.state, ex.completed_at = ExerciseState.completed, NOW
    db_session.flush()
    assert _enrol(client, course, w["student2"]).status_code == 201


@pytest.mark.parametrize("caller", ["admin", "instructor"])
def test_r2_a_finished_exercises_teams_are_frozen_for_everyone(client, db_session, world, caller):
    """Item 3: no update, delete or member removal on a completed or cancelled exercise's team."""
    from app.aar_access import took_part

    w = world
    for state in (ExerciseState.completed, ExerciseState.cancelled):
        ex = real_exercise(db_session, w["a"].id, state=state,
                           started_at=NOW - timedelta(days=2), completed_at=NOW - timedelta(days=1))
        _book(db_session, w["a"].id, w["instructor"], exercise=ex)  # a replay booking next week
        team = Team(id=uuid.uuid4(), name="Record", tenant_id=w["a"].id, exercise_id=ex.id)
        db_session.add(team)
        db_session.flush()
        db_session.add(TeamMembership(team_id=team.id, user_id=uuid.UUID(w["student"].id), joined_at=NOW))
        db_session.flush()
        act_as(w[caller])
        assert client.patch(f"/teams/{team.id}", json={"name": "x"}).status_code == 409
        assert client.delete(f"/teams/{team.id}/members/{w['student'].id}").status_code == 409
        assert client.delete(f"/teams/{team.id}").status_code == 409
        db_session.expire_all()
        assert db_session.query(TeamMembership).filter(TeamMembership.team_id == team.id).count() == 1
        if state == ExerciseState.completed:
            assert took_part(db_session, ex, uuid.UUID(w["student"].id)) is True


def _put_event(client, evt, **extra):
    body = {"name": evt.name, "start_time": evt.start_time.isoformat(), "end_time": evt.end_time.isoformat(),
            "course_id": str(evt.course_id) if evt.course_id else None, **extra}
    return client.put(f"/schedule/events/{evt.id}", json=body)


def _schedule_audits(db, actor, evt):
    db.expire_all()
    return [a for a in _audits(db, actor, "update") if a.resource_type == "schedule" and a.resource_id == str(evt.id)]


def test_r2_only_an_admin_moves_a_booking_between_instructors(client, db_session, world):
    """Item 4: taking a colleague's booking would take their class (scope follows the booking)."""
    w = world
    course = _course(db_session, w["a"].id, "Taken")
    evt = _book(db_session, w["a"].id, w["colleague"], course=course)
    evt.start_time, evt.end_time = NOW + timedelta(days=5), NOW + timedelta(days=5, hours=2)  # a free slot
    db_session.flush()
    act_as(w["instructor"])
    r = _put_event(client, evt, instructor_id=w["instructor"].id)
    assert r.status_code == 403, r.text
    assert _enrol(client, course, w["student"]).status_code == 403
    db_session.expire_all()
    assert db_session.get(ScheduledEvent, evt.id).instructor_id == uuid.UUID(w["colleague"].id)

    # The booking's own instructor hands it on; the audit row says who to whom.
    act_as(w["colleague"])
    assert _put_event(client, evt, instructor_id=w["instructor"].id).status_code == 200
    [row] = _schedule_audits(db_session, w["colleague"], evt)
    assert row.detail == f"instructor {w['colleague'].id} -> {w['instructor'].id}"

    # An admin moves it anywhere.
    act_as(w["admin"])
    assert _put_event(client, evt, instructor_id=w["colleague"].id).status_code == 200
    assert _schedule_audits(db_session, w["admin"], evt)


def test_r2_an_instructor_takes_an_unassigned_booking_only_for_themselves(client, db_session, world):
    w = world
    evt = _book(db_session, w["a"].id, w["admin"])
    evt.instructor_id = None
    evt.start_time, evt.end_time = NOW + timedelta(days=5), NOW + timedelta(days=5, hours=2)  # a free slot
    db_session.flush()
    act_as(w["instructor"])
    assert _put_event(client, evt, instructor_id=w["colleague"].id).status_code == 403
    assert _put_event(client, evt, instructor_id=w["instructor"].id).status_code == 200
    # Editing without naming an instructor leaves it as it is (no handover check to fail).
    assert _put_event(client, evt).status_code == 200
