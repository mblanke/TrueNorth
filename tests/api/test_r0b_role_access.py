"""R0-B role access: the API side of what each role's menu and pages offer.

The web app hides pages and buttons by the permissions in rbac.py
(control-plane/web/src/app/core/auth/permissions.ts). Hiding is not access control, so
every page a role is shown or denied is checked here against the API directly, with the
role's own identity, and across tenants:

- AAR: a Student reads only the report of a completed exercise they took part in
  (app/aar_access.py). It used to be any exercise in the tenant, running ones included.
- Resume: POST /exercises/{id}/resume moves paused to running once; a repeat or an
  exercise in another state is 409; Students and observers 403; another tenant's 404.
- Self-enrolment: POST /courses/{id}/enroll — published, own tenant or global catalogue,
  not retired; a repeat answers 200 with the same enrolment; someone else's needs
  user:update. GET /courses/enrollments/mine lists the caller's own only.
- Approvals queue: registration:read (instructors, admins); tenant-scoped.
- Observer: reads Ops Center and Scoring, writes neither.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.detections.models import DetectionSubmission
from app.models import (
    AfterActionReport,
    Course,
    Enrollment,
    Exercise,
    ExerciseState,
    RegistrationRequest,
    RegistrationStatus,
    Team,
    TeamMembership,
    UserRole,
)

STARTED = datetime(2026, 10, 1, 9, tzinfo=UTC)
AAR_PATHS = ("", "/html", "/pdf")


def _aar(db, ex: Exercise) -> None:
    report = {"exercise": {"id": str(ex.id), "name": ex.name, "state": ex.state.value}, "objectives": [],
              "injects": [], "timeline": [], "participants": [], "scores": {"total": 0, "max": 0, "pct": 0}}
    db.add(AfterActionReport(exercise_id=ex.id, report_json=json.dumps(report), report_html=""))
    db.flush()


def _on_team(db, ex: Exercise, user) -> None:
    team = Team(id=uuid.uuid4(), name="Blue", tenant_id=ex.tenant_id, exercise_id=ex.id)
    db.add(team)
    db.flush()
    db.add(TeamMembership(user_id=uuid.UUID(user.id), team_id=team.id, role="analyst"))
    db.flush()


def _submitted(db, ex: Exercise, user) -> None:
    db.add(DetectionSubmission(tenant_id=ex.tenant_id, exercise_id=ex.id, objective_ref="o", user_id=uuid.UUID(user.id),
                               query="q", verdict="missed", window_start=ex.started_at))  # in this run
    db.flush()


# ── AAR: a Student's own exercises only ───────────────────────────────────────────────
@pytest.fixture
def done(db_session):
    t = real_tenant(db_session, "aar-own")
    ex = real_exercise(db_session, t.id, state=ExerciseState.completed, started_at=STARTED, completed_at=STARTED)
    _aar(db_session, ex)
    return t, ex


@pytest.mark.parametrize("suffix", AAR_PATHS)
def test_a_student_on_the_exercise_team_reads_its_aar(client, db_session, done, suffix):
    t, ex = done
    me = real_user(db_session, UserRole.student, t.id)
    _on_team(db_session, ex, me)
    act_as(me)
    assert client.get(f"/exercises/{ex.id}/aar{suffix}").status_code == 200


def test_a_student_who_submitted_a_detection_reads_its_aar(client, db_session, done):
    t, ex = done
    me = real_user(db_session, UserRole.student, t.id)
    _submitted(db_session, ex, me)
    act_as(me)
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 200


@pytest.mark.parametrize("suffix", AAR_PATHS)
def test_a_student_who_took_no_part_gets_404(client, db_session, done, suffix):
    """Was 200: every Student in the tenant read every report, classmates' evidence included."""
    t, ex = done
    classmate = real_user(db_session, UserRole.student, t.id)
    _on_team(db_session, ex, classmate)
    act_as(real_user(db_session, UserRole.student, t.id))
    assert client.get(f"/exercises/{ex.id}/aar{suffix}").status_code == 404


def test_a_participant_cannot_read_the_aar_of_a_run_still_in_progress(client, db_session):
    """A mid-run report carries the objectives' evidence and the inject playbook: the answer key."""
    t = real_tenant(db_session, "aar-running")
    ex = real_exercise(db_session, t.id, state=ExerciseState.running, started_at=STARTED)
    _aar(db_session, ex)
    me = real_user(db_session, UserRole.student, t.id)
    _on_team(db_session, ex, me)
    act_as(me)
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 404


def test_a_participant_in_another_tenant_gets_404(client, db_session, done):
    _, ex = done
    other = real_tenant(db_session, "aar-other")
    me = real_user(db_session, UserRole.student, other.id)
    _on_team(db_session, ex, me)  # even a stray membership row does not cross tenants
    act_as(me)
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 404


@pytest.mark.parametrize("role", [UserRole.observer, UserRole.instructor, UserRole.admin])
def test_reviewers_read_every_report_in_their_tenant(client, db_session, done, role):
    t, ex = done
    act_as(real_user(db_session, role, t.id))
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 200


def test_reviewers_do_not_read_another_tenants_report(client, db_session, done):
    _, ex = done
    act_as(real_user(db_session, UserRole.observer, real_tenant(db_session, "aar-obs-other").id))
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 404


def test_range_ops_hold_no_aar_read(client, db_session, done):
    t, ex = done
    act_as(real_user(db_session, UserRole.range_ops, t.id))
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 403


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer])
def test_only_staff_generate_an_aar(client, db_session, done, role):
    t, ex = done
    act_as(real_user(db_session, role, t.id))
    assert client.post(f"/exercises/{ex.id}/aar/generate").status_code == 403


# ── Resume ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def paused(db_session):
    t = real_tenant(db_session, "resume")
    return t, real_exercise(db_session, t.id, state=ExerciseState.paused, started_at=STARTED)


def _state(db, ex) -> ExerciseState:
    db.expire_all()
    return db.get(Exercise, ex.id).state


def test_resume_moves_paused_to_running_once(client, db_session, paused):
    t, ex = paused
    act_as(real_user(db_session, UserRole.instructor, t.id))
    first = client.post(f"/exercises/{ex.id}/resume")
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "running"
    # A double click: the second finds it running and changes nothing.
    second = client.post(f"/exercises/{ex.id}/resume")
    assert second.status_code == 409
    assert "expected paused" in second.json()["detail"]
    assert _state(db_session, ex) == ExerciseState.running


@pytest.mark.parametrize("state", [ExerciseState.pending, ExerciseState.completed])
def test_resume_refuses_an_exercise_that_is_not_paused(client, db_session, state):
    t = real_tenant(db_session, f"resume-{state.value}")
    ex = real_exercise(db_session, t.id, state=state, started_at=STARTED)
    act_as(real_user(db_session, UserRole.instructor, t.id))
    r = client.post(f"/exercises/{ex.id}/resume")
    assert r.status_code == 409
    assert r.json()["detail"] == f"Exercise is {state.value}, expected paused"
    assert _state(db_session, ex) == state


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_resume_is_refused_to_roles_without_exercise_pause(client, db_session, paused, role):
    t, ex = paused
    act_as(real_user(db_session, role, t.id))
    assert client.post(f"/exercises/{ex.id}/resume").status_code == 403
    assert _state(db_session, ex) == ExerciseState.paused


def test_resume_of_another_tenants_exercise_is_404(client, db_session, paused):
    _, ex = paused
    act_as(real_user(db_session, UserRole.instructor, real_tenant(db_session, "resume-other").id))
    assert client.post(f"/exercises/{ex.id}/resume").status_code == 404
    assert _state(db_session, ex) == ExerciseState.paused


# ── Self-enrolment ────────────────────────────────────────────────────────────────────
@pytest.fixture
def catalogue(db_session):
    t, other = real_tenant(db_session, "enrol"), real_tenant(db_session, "enrol-other")
    rows = {
        "mine": Course(id=uuid.uuid4(), name="Mine", tenant_id=t.id, is_published=True),
        "global": Course(id=uuid.uuid4(), name="Shared", tenant_id=None, is_published=True),
        "draft": Course(id=uuid.uuid4(), name="Draft", tenant_id=t.id, is_published=False),
        "theirs": Course(id=uuid.uuid4(), name="Theirs", tenant_id=other.id, is_published=True),
        "retired": Course(id=uuid.uuid4(), name="Stub", tenant_id=t.id, is_published=True,
                          course_meta=json.dumps({"retired": True})),
    }
    db_session.add_all(rows.values())
    db_session.flush()
    return t, other, rows


def _enrol(client, course, user):
    return client.post(f"/courses/{course.id}/enroll", json={"user_id": user.id, "course_id": str(course.id)})


def test_a_student_enrols_and_a_repeat_returns_the_same_enrolment(client, db_session, catalogue):
    t, _, rows = catalogue
    me = real_user(db_session, UserRole.student, t.id)
    act_as(me)
    first = _enrol(client, rows["mine"], me)
    assert first.status_code == 201, first.text
    again = _enrol(client, rows["mine"], me)
    assert again.status_code == 200, again.text  # was 409: a second click read as a failure
    assert again.json()["id"] == first.json()["id"]
    assert db_session.query(Enrollment).filter(Enrollment.user_id == uuid.UUID(me.id)).count() == 1


def test_a_student_enrols_on_the_shared_catalogue(client, db_session, catalogue):
    """Global courses (tenant NULL) were 404 here, so the standard programme was not enrollable."""
    t, _, rows = catalogue
    me = real_user(db_session, UserRole.student, t.id)
    act_as(me)
    r = _enrol(client, rows["global"], me)
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == str(t.id)


@pytest.mark.parametrize(("key", "status"), [("draft", 404), ("theirs", 404), ("retired", 409)])
def test_restricted_courses_stay_restricted(client, db_session, catalogue, key, status):
    t, _, rows = catalogue
    me = real_user(db_session, UserRole.student, t.id)
    act_as(me)
    assert _enrol(client, rows[key], me).status_code == status
    assert db_session.query(Enrollment).filter(Enrollment.user_id == uuid.UUID(me.id)).count() == 0


def test_a_student_cannot_enrol_a_classmate(client, db_session, catalogue):
    t, _, rows = catalogue
    classmate = real_user(db_session, UserRole.student, t.id)
    act_as(real_user(db_session, UserRole.student, t.id))
    assert _enrol(client, rows["mine"], classmate).status_code == 403


def test_my_enrolments_are_mine_only(client, db_session, catalogue):
    t, _, rows = catalogue
    me, classmate = (real_user(db_session, UserRole.student, t.id) for _ in range(2))
    act_as(classmate)
    assert _enrol(client, rows["global"], classmate).status_code == 201
    act_as(me)
    assert client.get("/courses/enrollments/mine").json() == []
    assert _enrol(client, rows["mine"], me).status_code == 201
    mine = client.get("/courses/enrollments/mine").json()
    assert [e["course_id"] for e in mine] == [str(rows["mine"].id)]


# ── Approvals queue (registration:read) ───────────────────────────────────────────────
def _request(db, tenant_id) -> RegistrationRequest:
    sub = f"ad-{uuid.uuid4().hex[:8]}"
    row = RegistrationRequest(id=uuid.uuid4(), keycloak_id=sub, email=f"{sub}@corp.example", display_name=sub,
                              suggested_tenant_id=tenant_id, status=RegistrationStatus.pending)
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_the_queue_is_refused_without_registration_read(client, db_session, role):
    t = real_tenant(db_session, f"queue-{role.value}")
    req = _request(db_session, t.id)
    act_as(real_user(db_session, role, t.id))
    assert client.get("/registration/requests").status_code == 403
    assert client.post(f"/registration/requests/{req.id}/approve", json={"role": "student"}).status_code == 403


def test_an_instructor_works_their_own_tenants_queue_only(client, db_session):
    mine_t, other_t = real_tenant(db_session, "queue-a"), real_tenant(db_session, "queue-b")
    mine, theirs = _request(db_session, mine_t.id), _request(db_session, other_t.id)
    act_as(real_user(db_session, UserRole.instructor, mine_t.id))
    ids = {r["id"] for r in client.get("/registration/requests").json()}
    assert str(mine.id) in ids and str(theirs.id) not in ids
    assert client.post(f"/registration/requests/{theirs.id}/approve", json={"role": "student"}).status_code == 404
    ok = client.post(f"/registration/requests/{mine.id}/approve", json={"role": "student"})
    assert ok.status_code == 200, ok.text


# ── Observer: Ops Center and Scoring, read only ───────────────────────────────────────
def test_an_observer_reads_ops_center_and_scoring_but_writes_neither(client, db_session):
    t = real_tenant(db_session, "observer")
    ex = real_exercise(db_session, t.id, state=ExerciseState.running, started_at=STARTED)
    act_as(real_user(db_session, UserRole.observer, t.id))
    assert client.get(f"/ops/exercises/{ex.id}/annotations").status_code == 200
    assert client.get(f"/exercises/{ex.id}/objectives").status_code == 200
    assert client.post(f"/ops/exercises/{ex.id}/annotations",
                       json={"content": "x", "annotation_type": "observation", "severity": "info"}).status_code == 403
    assert client.post(f"/exercises/{ex.id}/pause").status_code == 403
    assert client.post(f"/exercises/{ex.id}/objectives/o/ack", json={"evidence": ""}).status_code == 403
