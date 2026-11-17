"""Team exercises: Student detections are evidence, instructors judge (ADR 0005, ADR 0020).

There is no answer key. A detection is run inside the exercise window (the server's ingest
clock) and recorded with what it matched; it credits nothing. An objective is achieved only
by an instructor's judgement, which may cite the detections it relied on.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import (
    DEV_TENANT,
    OTHER_TENANT,
    SCENARIO,
    STARTED,
    FakeStore,
    _at,
    acting_as,
    beacon,
    noise,
)
from app.detections.models import DetectionSubmission
from app.main import app as fastapi_app
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, Scenario, UserRole
from app.rbac import ROLE_PERMISSIONS, Permission
from app.routers.detections import search_backend
from app.search_backends import SearchQueryError


@pytest.fixture(autouse=True)
def _any_tenant_student(monkeypatch):
    """These tests are about recording and judging, not who takes part: exercises here have no roster
    (participation: tests/api/test_detection_participants.py)."""
    monkeypatch.setenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", "false")


@pytest.fixture
def store():
    s = FakeStore()
    fastapi_app.dependency_overrides[search_backend] = lambda: s
    yield s
    fastapi_app.dependency_overrides.pop(search_backend, None)


def _exercise(db, *, tenant=DEV_TENANT, state=ExerciseState.running, started=STARTED) -> Exercise:
    sc = Scenario(id=uuid.uuid4(), name=f"sc-{uuid.uuid4().hex[:6]}", yaml=SCENARIO, tenant_id=uuid.UUID(tenant))
    ex = Exercise(id=uuid.uuid4(), name="ex", range_id=uuid.uuid4(), scenario_id=sc.id, tenant_id=uuid.UUID(tenant),
                  state=state, started_at=started, max_score=60, total_score=0)
    db.add_all([sc, ex])
    for ref, validator, points in (("detect_c2", "opensearch_query", 40), ("detect_phish", "validate.opensearch_query", 10),
                                   ("undefined_var", "opensearch_query", 5), ("write_report", "deliverable_check", 5)):
        # Rows carry no params (as QSP-made rows do): look-for guidance comes from the scenario.
        db.add(Objective(exercise_id=ex.id, ref_id=ref, objective_type=ObjectiveType.detection,
                         description=ref, validator=validator, points=points))
    db.commit()
    return ex


def _submit(client, ex, query, ref="detect_c2"):
    return client.post(f"/exercises/{ex.id}/objectives/{ref}/detections", json={"query": query})


def _obj(db, ex, ref="detect_c2") -> Objective:
    db.expire_all()
    return db.query(Objective).filter(Objective.exercise_id == ex.id, Objective.ref_id == ref).one()


def test_students_and_instructors_submit_observers_do_not():
    holders = {r for r, p in ROLE_PERMISSIONS.items() if Permission.DETECTION_SUBMIT in p}
    assert holders == {UserRole.admin, UserRole.instructor, UserRole.student}


class TestRecording:
    def test_an_idle_student_scores_nothing_however_much_attack_telemetry_exists(self, client, db_session, store):
        store.events = [beacon() for _ in range(50)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert client.get(f"/exercises/{ex.id}/detections").json() == []
        assert _obj(db_session, ex).achieved is False

    def test_a_detection_is_recorded_with_what_it_matched_and_credits_nothing(self, client, db_session, store):
        # ADR 0020: no answer key, no verdict. A detection that finds exactly the attack
        # is evidence for the instructor, never an achieved objective by itself.
        store.events = [beacon(), beacon(), *noise(n=3)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as who:
            resp = _submit(client, ex, "url.domain:*northwind-update.example")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert (body["verdict"], body["events_matched"], body["submissions_left"]) == ("recorded", 2, 19)
        assert "on_target" not in body and "precision" not in body
        assert _obj(db_session, ex).achieved is False
        assert db_session.get(Exercise, ex.id).total_score == 0
        row = db_session.get(DetectionSubmission, uuid.UUID(body["id"]))
        assert str(row.user_id) == who.id and json.loads(row.matched_ids) == ["0", "1"]
        assert (row.threshold, row.min_precision, row.on_target) == (None, None, None)
        assert store.indices == {f"range-{ex.range_id},range-{ex.range_id}-*"}

    def test_a_catch_all_is_recorded_too_the_instructor_sees_what_it_matched(self, client, db_session, store):
        store.events = [beacon(), *noise(n=10)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            body = _submit(client, ex, "*").json()
        assert (body["verdict"], body["events_matched"]) == ("recorded", 11)
        assert _obj(db_session, ex).achieved is False

    def test_the_window_excludes_telemetry_before_the_start_and_without_server_time(self, client, db_session, store):
        unstamped = {"event_type": "http", "url.domain": "cdn.northwind-update.example", "@timestamp": _at(5)}
        store.events = [beacon(minutes=-30), beacon(minutes=3), unstamped]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "url.domain:*northwind*").json()["events_matched"] == 1

    def test_any_spelling_of_the_query_validator_is_a_detection_objective(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "attachment.name:*.docm", ref="detect_phish").status_code == 201


class TestRefusals:
    @pytest.mark.parametrize("state", [ExerciseState.pending, ExerciseState.completed, ExerciseState.paused])
    def test_only_while_running(self, client, db_session, store, state):
        ex = _exercise(db_session, state=state)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "a:b").status_code == 409

    def test_other_tenant_is_not_found(self, client, db_session, store):
        ex = _exercise(db_session, tenant=OTHER_TENANT)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "a:b").status_code == 404

    def test_observer_cannot_submit(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.observer):
            assert _submit(client, ex, "a:b").status_code == 403

    def test_an_objective_not_worked_by_a_detection_takes_none(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            resp = _submit(client, ex, "a:b", ref="write_report")
        assert resp.status_code == 409 and "not worked by a detection" in resp.json()["detail"]

    def test_an_objective_with_no_look_for_still_takes_detections(self, client, db_session, store):
        # There is no key to need: undefined_var's guidance does not render, which no longer matters.
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "a:b", ref="undefined_var").status_code == 201

    def test_none_after_the_instructor_judged_it_achieved(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            assert _ack(client, ex).status_code == 200
        with acting_as(UserRole.student):
            assert _submit(client, ex, "a:b").status_code == 409

    def test_submissions_are_bounded_per_student_per_run(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            lefts = [_submit(client, ex, "nope:x").json()["submissions_left"] for _ in range(20)]
            assert lefts[0] == 19 and lefts[-1] == 0
            assert _submit(client, ex, "nope:x").status_code == 429
        with acting_as(UserRole.student):  # another Student has their own bound
            assert _submit(client, ex, "nope:x").status_code == 201

    def test_a_pending_submission_counts_and_a_long_dead_one_does_not(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as who:
            for age in (timedelta(0), timedelta(hours=1)):
                db_session.add(DetectionSubmission(exercise_id=ex.id, objective_ref="detect_c2", query="x",
                                                   user_id=uuid.UUID(who.id), verdict="pending",
                                                   window_start=STARTED, submitted_at=datetime.now(UTC) - age))
            db_session.commit()
            assert _submit(client, ex, "nope:x").json()["submissions_left"] == 18

    def test_a_replayed_run_starts_its_bound_again(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            for _ in range(20):
                _submit(client, ex, "nope:x")
            assert _submit(client, ex, "nope:x").status_code == 429
        ex = db_session.get(Exercise, ex.id)
        ex.started_at = STARTED + timedelta(hours=1)
        db_session.commit()
        with acting_as(UserRole.student):
            assert _submit(client, ex, "nope:x").json()["submissions_left"] == 19

    def test_a_query_the_store_cannot_parse_is_422_and_not_recorded(self, client, db_session):
        class Refuses(FakeStore):
            async def match(self, index, query, size=0):
                raise SearchQueryError("Failed to parse query [bytes_out:>1]")

        fastapi_app.dependency_overrides[search_backend] = lambda: Refuses()
        ex = _exercise(db_session)
        try:
            with acting_as(UserRole.student):
                resp = _submit(client, ex, "bytes_out:>1")
            assert resp.status_code == 422 and "nothing recorded" in resp.json()["detail"]
        finally:
            fastapi_app.dependency_overrides.pop(search_backend, None)
        assert db_session.query(DetectionSubmission).count() == 0

    def test_a_store_outage_is_503_and_not_recorded(self, client, db_session):
        from app.search_backends import NullSearchBackend

        fastapi_app.dependency_overrides[search_backend] = lambda: NullSearchBackend()
        ex = _exercise(db_session)
        try:
            with acting_as(UserRole.student):
                assert _submit(client, ex, "a:b").status_code == 503
        finally:
            fastapi_app.dependency_overrides.pop(search_backend, None)
        assert db_session.query(DetectionSubmission).count() == 0


class TestSubmissionsList:
    def test_students_see_their_own_staff_see_all(self, client, db_session, store):
        store.events = [beacon(), *noise(n=3)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            _submit(client, ex, "event_type:http")
        with acting_as(UserRole.student):
            _submit(client, ex, "nope:x")
            mine = client.get(f"/exercises/{ex.id}/detections").json()
        assert [d["query"] for d in mine] == ["nope:x"]
        with acting_as(UserRole.instructor):
            every = client.get(f"/exercises/{ex.id}/detections").json()
        assert sorted((d["query"], d["events_matched"]) for d in every) == [("event_type:http", 4), ("nope:x", 0)]

    def test_other_tenant_list_is_not_found(self, client, db_session, store):
        ex = _exercise(db_session, tenant=OTHER_TENANT)
        with acting_as(UserRole.instructor):
            assert client.get(f"/exercises/{ex.id}/detections").status_code == 404


def _ack(client, ex, ref="detect_c2", **body):
    return client.post(f"/exercises/{ex.id}/objectives/{ref}/ack", json=body or None)


class TestJudging:
    """ADR 0020: an instructor's judgement is the only way an exercise objective is achieved."""

    def test_an_instructor_judges_with_a_note_and_cites_the_detections_relied_on(self, client, db_session, store):
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as student:
            cited = _submit(client, ex, "url.domain:*northwind*").json()["id"]
        with acting_as(UserRole.instructor) as instructor:
            resp = _ack(client, ex, evidence="Blue team found the beacon at 12:05", cites=[cited])
        assert resp.status_code == 200, resp.text
        obj = _obj(db_session, ex)
        ev = json.loads(obj.evidence)
        assert obj.achieved and ev["source"] == "instructor" and ev["by_id"] == instructor.id
        assert (ev["note"], ev["cites"], ev["students"]) == ("Blue team found the beacon at 12:05", [cited], [student.id])
        assert db_session.get(Exercise, ex.id).total_score == 40
        assert db_session.get(DetectionSubmission, uuid.UUID(cited)).verdict == "cited"

    def test_a_judgement_needs_no_citation(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            assert _ack(client, ex, ref="write_report").status_code == 200
        assert json.loads(_obj(db_session, ex, "write_report").evidence)["cites"] == []

    def test_the_json_body_note_the_web_app_sends_is_kept(self, client, db_session, store):
        # The web app and SDK always sent {"evidence": ...} as JSON; the server read a query
        # parameter and dropped it. Now the JSON body is what counts.
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            client.post(f"/exercises/{ex.id}/objectives/detect_c2/ack", json={"evidence": "seen live"})
        assert json.loads(_obj(db_session, ex).evidence)["note"] == "seen live"

    @pytest.mark.parametrize("which", ["other_objective", "other_exercise", "unknown"])
    def test_only_this_objectives_detections_in_this_run_can_be_cited(self, client, db_session, store, which):
        ex, other = _exercise(db_session), _exercise(db_session)
        with acting_as(UserRole.student):
            ids = {
                "other_objective": _submit(client, ex, "a:b", ref="detect_phish").json()["id"],
                "other_exercise": _submit(client, other, "a:b").json()["id"],
                "unknown": str(uuid.uuid4()),
            }
        with acting_as(UserRole.instructor):
            assert _ack(client, ex, cites=[ids[which]]).status_code == 422
        assert _obj(db_session, ex).achieved is False

    def test_a_detection_from_an_earlier_run_cannot_be_cited(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            old = _submit(client, ex, "a:b").json()["id"]
        db_session.get(Exercise, ex.id).started_at = STARTED + timedelta(hours=1)
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert _ack(client, ex, cites=[old]).status_code == 422

    def test_students_and_observers_cannot_judge(self, client, db_session, store):
        ex = _exercise(db_session)
        for role in (UserRole.student, UserRole.observer):
            with acting_as(role):
                assert _ack(client, ex).status_code == 403
        assert _obj(db_session, ex).achieved is False

    def test_withdrawing_a_judgement_re_totals_and_uncites(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            cited = _submit(client, ex, "a:b").json()["id"]
        with acting_as(UserRole.instructor):
            _ack(client, ex, cites=[cited])
            resp = client.delete(f"/exercises/{ex.id}/objectives/detect_c2/ack")
            assert resp.status_code == 200, resp.text
            assert client.delete(f"/exercises/{ex.id}/objectives/detect_c2/ack").status_code == 409
        obj = _obj(db_session, ex)
        assert (obj.achieved, obj.evidence) == (False, None)
        assert db_session.get(Exercise, ex.id).total_score == 0
        assert db_session.get(DetectionSubmission, uuid.UUID(cited)).verdict == "recorded"

    @pytest.mark.parametrize("state", [ExerciseState.completed, ExerciseState.cancelled, ExerciseState.pending])
    def test_judging_is_refused_once_the_exercise_is_closed(self, client, db_session, store, state):
        ex = _exercise(db_session, state=state)
        with acting_as(UserRole.instructor):
            assert _ack(client, ex).status_code == 409
            assert client.delete(f"/exercises/{ex.id}/objectives/detect_c2/ack").status_code == 409

    def test_other_tenant_cannot_judge(self, client, db_session, store):
        ex = _exercise(db_session, tenant=OTHER_TENANT)
        with acting_as(UserRole.instructor):
            assert _ack(client, ex).status_code == 404
            assert client.delete(f"/exercises/{ex.id}/objectives/detect_c2/ack").status_code == 404


class TestClosing:
    def test_complete_assesses_the_exercises_students_not_whoever_submitted(
        self, client, db_session, store, monkeypatch
    ):
        # M7: auto-assessment, the LTI grade and xAPI go to the exercise's Students (here its
        # team). A recorded detection is evidence, not credit (ADR 0020): a submitter who is
        # not one of them is not assessed, nor is the instructor who completed it (#375).
        from app import celery_client
        from app.models import Team, TeamMembership, User

        dispatched = []
        monkeypatch.setattr(celery_client, "dispatch", lambda name, *args: dispatched.append((name, args)) or "id")
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        students = []
        for i in range(3):
            u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4().hex[:6]}", email=f"s{i}@example.org",
                     display_name=f"Student {i}", tenant_id=uuid.UUID(DEV_TENANT), role=UserRole.student)
            db_session.add(u)
            students.append(u)
        team = Team(name=f"blue-{uuid.uuid4().hex[:4]}", tenant_id=uuid.UUID(DEV_TENANT), exercise_id=ex.id)
        db_session.add(team)
        db_session.flush()
        for u in students[:2]:
            db_session.add(TeamMembership(user_id=u.id, team_id=team.id))
        db_session.commit()
        for u in (students[0], students[2]):
            with acting_as(UserRole.student) as who:
                who.id = str(u.id)
                _submit(client, ex, "nope:x")
        with acting_as(UserRole.instructor):
            assert client.post(f"/exercises/{ex.id}/complete").status_code == 200
        assessed = {args[1] for name, args in dispatched if name == "auto_assess_competency"}
        assert assessed == {str(students[0].id), str(students[1].id)}

    def test_the_clock_closes_running_and_paused_exercises_past_their_duration(self, db_session, monkeypatch):
        from app import celery_client
        from app.exercise_completion import sweep_overdue

        monkeypatch.setattr(celery_client, "dispatch", lambda *a: "id")
        now = STARTED + timedelta(minutes=120)
        timed = SCENARIO.replace('name: s\n', 'name: s\nduration_minutes: 90\n')
        overdue = _exercise(db_session)
        paused = _exercise(db_session, state=ExerciseState.paused)
        fresh = _exercise(db_session, started=STARTED + timedelta(minutes=60))
        untimed = _exercise(db_session)
        for ex in (overdue, paused, fresh):
            db_session.get(Scenario, ex.scenario_id).yaml = timed
        db_session.commit()

        closed = set(sweep_overdue(db_session, now))

        assert closed == {overdue.id, paused.id}
        db_session.expire_all()
        assert db_session.get(Exercise, fresh.id).state == ExerciseState.running
        assert db_session.get(Exercise, untimed.id).state == ExerciseState.running
        assert db_session.get(Exercise, overdue.id).completed_at is not None


class TestJudgingReviewFindings:
    """Adversarial review of ADR 0020's first build (2026-11-04)."""

    def test_the_lock_rereads_the_exercise_so_a_close_in_between_refuses_the_ack(self, db_session):
        # M1: get_owned had loaded the exercise (running); another session closed it; the
        # lock must see "completed", not the stale copy, or the ack lands after results went out.
        from app import exercise_judging
        from app.exercise_judging import JudgementIn
        from fastapi import HTTPException

        ex = _exercise(db_session)
        stale = db_session.get(Exercise, ex.id)
        assert stale.state == ExerciseState.running
        db_session.execute(Exercise.__table__.update().where(Exercise.id == ex.id).values(state="completed"))
        with acting_as(UserRole.instructor) as who, pytest.raises(HTTPException) as refused:
            exercise_judging.acknowledge(db_session, ex.id, "detect_c2", JudgementIn(), who)
        assert refused.value.status_code == 409
        assert _obj(db_session, ex).achieved is False

    def test_max_score_follows_the_objectives_when_the_exercise_was_made_without_one(self, client, db_session, store):
        # M2: QSP-path exercises start with max_score 0; the worker used to set it at completion.
        ex = _exercise(db_session)
        db_session.get(Exercise, ex.id).max_score = 0
        db_session.commit()
        with acting_as(UserRole.instructor):
            _ack(client, ex)
            assert client.post(f"/exercises/{ex.id}/complete").status_code == 200
        db_session.expire_all()
        e = db_session.get(Exercise, ex.id)
        assert (e.total_score, e.max_score) == (40, 60)

    def test_a_maximum_someone_set_is_never_rewritten(self, client, db_session, store):
        ex = _exercise(db_session)  # max_score 60 set at creation; objectives sum 60 here too
        db_session.get(Exercise, ex.id).max_score = 100
        db_session.commit()
        with acting_as(UserRole.instructor):
            _ack(client, ex)
            client.post(f"/exercises/{ex.id}/complete")
        db_session.expire_all()
        assert db_session.get(Exercise, ex.id).max_score == 100

    def test_withdrawing_pre_adr_0020_credit_uncredits_its_submission(self, client, db_session, store):
        # m1: an exercise open at deploy time may hold automatic credit pointing at one submission.
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as who:
            sid = uuid.uuid4()
            db_session.add(DetectionSubmission(id=sid, tenant_id=ex.tenant_id, exercise_id=ex.id,
                                               objective_ref="detect_c2", user_id=uuid.UUID(who.id), query="q",
                                               verdict="achieved", window_start=STARTED))
        obj = _obj(db_session, ex)
        obj.achieved = True
        obj.evidence = json.dumps({"source": "student_detection", "submission_id": str(sid)})
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert client.delete(f"/exercises/{ex.id}/objectives/detect_c2/ack").status_code == 200
        assert db_session.get(DetectionSubmission, sid).verdict == "recorded"

    def test_a_replay_uncites_the_last_runs_submissions(self, client, db_session, store):
        # m4: replay wipes the judgements; their citations must not keep crediting anyone.
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            cited = _submit(client, ex, "a:b").json()["id"]
        with acting_as(UserRole.instructor):
            _ack(client, ex, cites=[cited])
            client.post(f"/exercises/{ex.id}/complete")
            client.post(f"/exercises/{ex.id}/run", params={"reset": "true"})
        db_session.expire_all()
        assert db_session.get(DetectionSubmission, uuid.UUID(cited)).verdict == "recorded"
