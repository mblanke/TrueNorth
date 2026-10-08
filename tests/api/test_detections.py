"""Detection credit comes from what the Student did (ADR 0005, review finding 1).

The scenario query is the answer key. An idle Student scores nothing; a Student's query
is credited only when it finds enough of the attack, precisely enough, inside the
exercise window measured on the server's ingest clock.
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
        # Rows carry no params (as QSP-made rows do): the answer key comes from the scenario.
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


class TestCredit:
    def test_an_idle_student_scores_nothing_however_much_attack_telemetry_exists(self, client, db_session, store):
        # The blocker: the inject's own telemetry used to achieve the objective by itself.
        store.events = [beacon() for _ in range(50)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert client.get(f"/exercises/{ex.id}/detections").json() == []
        assert _obj(db_session, ex).achieved is False

    def test_a_precise_detection_is_credited_with_the_student_named(self, client, db_session, store):
        store.events = [beacon(), beacon(), *noise(n=3)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as student:
            resp = _submit(client, ex, "url.domain:*northwind-update.example")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["verdict"] == "achieved" and body["events_matched"] == 2 and body["attempts_left"] == 4
        assert body["on_target"] is None and body["precision"] is None  # Students do not see the key's numbers
        obj = _obj(db_session, ex)
        evidence = json.loads(obj.evidence)
        assert obj.achieved and evidence["source"] == "student_detection"
        assert evidence["student_id"] == student.id and evidence["submission_id"] == body["id"]
        assert "query" not in evidence  # the answer key never lands where Students can read it
        assert db_session.get(Exercise, ex.id).total_score == 40
        assert store.indices == {f"range-{ex.range_id},range-{ex.range_id}-*"}

    def test_a_catch_all_query_fails_the_precision_floor(self, client, db_session, store):
        store.events = [beacon(), beacon(), *noise(n=10)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            body = _submit(client, ex, "*").json()
        assert body["verdict"] == "missed" and body["events_matched"] == 12
        assert _obj(db_session, ex).achieved is False

    def test_too_few_attack_events_found_is_missed(self, client, db_session, store):
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "url.domain:cdn.northwind-update.example AND nope:x").json()["verdict"] == "missed"

    def test_telemetry_before_the_exercise_started_does_not_count(self, client, db_session, store):
        # An earlier exercise on the same (reused) range, or the inject before start.
        store.events = [beacon(minutes=-30), beacon(minutes=-1), beacon(minutes=3)]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            body = _submit(client, ex, "url.domain:*northwind*").json()
        assert body["verdict"] == "missed" and body["events_matched"] == 1  # min_hits is 2

    def test_events_without_a_server_ingest_time_do_not_count(self, client, db_session, store):
        store.events = [{"event_type": "http", "url.domain": "cdn.northwind-update.example", "@timestamp": _at(5)}] * 3
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "url.domain:*northwind*").json()["events_matched"] == 0

    def test_old_validator_spelling_is_still_a_detection_objective(self, client, db_session, store):
        store.events = [{"event_type": "email", "attachment.name": "q1.docm", "truenorth": {"ingested_at": _at(1)}}]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "attachment.name:*.docm", ref="detect_phish").json()["verdict"] == "achieved"


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

    @pytest.mark.parametrize(("ref", "why"), [("undefined_var", "attacker_ip"), ("write_report", "not a detection")])
    def test_unscorable_objectives_say_why(self, client, db_session, store, ref, why):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            resp = _submit(client, ex, "a:b", ref=ref)
        assert resp.status_code == 409 and why in resp.json()["detail"]

    def test_already_achieved(self, client, db_session, store):
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "url.domain:*northwind*").status_code == 201
            assert _submit(client, ex, "url.domain:*northwind*").status_code == 409

    def test_attempts_are_capped_per_student(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            for left in (4, 3, 2, 1, 0):
                assert _submit(client, ex, "nope:x").json()["attempts_left"] == left
            assert _submit(client, ex, "nope:x").status_code == 429
        with acting_as(UserRole.student):  # another Student on the team has their own attempts
            assert _submit(client, ex, "nope:x").status_code == 201

    def test_a_store_outage_is_unscored_and_costs_no_attempt(self, client, db_session, store):
        store.down = True
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            assert _submit(client, ex, "a:b").status_code == 503
            store.down = False
            assert _submit(client, ex, "nope:x").json()["attempts_left"] == 4
        verdicts = [r.verdict for r in db_session.query(DetectionSubmission).order_by(DetectionSubmission.submitted_at)]
        assert sorted(verdicts) == ["missed", "unscored"]
        assert _obj(db_session, ex).achieved is False


class TestAttemptsList:
    def test_students_see_their_own_staff_see_all_with_precision(self, client, db_session, store):
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
        assert len(every) == 2
        http = next(d for d in every if d["query"] == "event_type:http")
        assert (http["events_matched"], http["on_target"], http["precision"]) == (4, 1, 0.25)

    def test_other_tenant_list_is_not_found(self, client, db_session, store):
        ex = _exercise(db_session, tenant=OTHER_TENANT)
        with acting_as(UserRole.instructor):
            assert client.get(f"/exercises/{ex.id}/detections").status_code == 404


# -- review findings (adversarial review of 2026-10-06) ---------------------------------
class _Hook(FakeStore):
    """A store that runs ``during`` while it is judging, as a concurrent request would."""

    def __init__(self, events, during):
        super().__init__(events)
        self.during = during

    async def match(self, index, query, size=0):
        if self.during:
            hook, self.during = self.during, None
            hook()
        return await super().match(index, query, size)


class TestReviewFindings:
    def test_a_pending_attempt_counts_so_concurrent_submissions_cannot_exceed_the_cap(self, client, db_session, store):
        # M1: the attempt is reserved before judging; a request still being judged counts.
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as who:
            for _ in range(4):
                db_session.add(DetectionSubmission(exercise_id=ex.id, objective_ref="detect_c2", query="x",
                                                   user_id=uuid.UUID(who.id), verdict="pending",
                                                   window_start=STARTED, submitted_at=datetime.now(UTC)))
            db_session.commit()
            assert _submit(client, ex, "nope:x").json()["attempts_left"] == 0
            assert _submit(client, ex, "nope:x").status_code == 429

    def test_a_long_dead_pending_attempt_is_freed(self, client, db_session, store):
        ex = _exercise(db_session)
        with acting_as(UserRole.student) as who:
            db_session.add(DetectionSubmission(exercise_id=ex.id, objective_ref="detect_c2", query="x",
                                               user_id=uuid.UUID(who.id), verdict="pending", window_start=STARTED,
                                               submitted_at=datetime.now(UTC) - timedelta(hours=1)))
            db_session.commit()
            assert _submit(client, ex, "nope:x").json()["attempts_left"] == 4

    def test_no_credit_if_the_exercise_closes_while_judging(self, client, db_session):
        # M2: results may already have gone out; the late credit must not move the score.
        ex = _exercise(db_session)

        def instructor_completes():
            db_session.query(Exercise).filter(Exercise.id == ex.id).update({"state": ExerciseState.completed})
            db_session.commit()

        store = _Hook([beacon(), beacon()], instructor_completes)
        fastapi_app.dependency_overrides[search_backend] = lambda: store
        try:
            with acting_as(UserRole.student):
                body = _submit(client, ex, "url.domain:*northwind*").json()
        finally:
            fastapi_app.dependency_overrides.pop(search_backend, None)
        assert body["verdict"] == "closed"
        assert _obj(db_session, ex).achieved is False
        assert db_session.get(Exercise, ex.id).total_score == 0

    def test_a_replayed_run_starts_with_full_attempts_and_no_old_evidence(self, client, db_session, store):
        # M3: attempts belong to a run; /run resets evidence along with achieved.
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            for _ in range(5):
                _submit(client, ex, "nope:x")
            assert _submit(client, ex, "nope:x").status_code == 429
        ex = db_session.get(Exercise, ex.id)
        ex.started_at = STARTED + timedelta(hours=1)  # the replay's start
        db_session.commit()
        for e in store.events:
            e["truenorth"]["ingested_at"] = _at(65)
        with acting_as(UserRole.student):
            body = _submit(client, ex, "url.domain:*northwind*").json()
        assert body["verdict"] == "achieved" and body["attempts_left"] == 4

    def test_run_reset_clears_the_last_runs_evidence(self, client, db_session, store):
        ex = _exercise(db_session, state=ExerciseState.completed)
        obj = _obj(db_session, ex)
        obj.achieved, obj.evidence = True, '{"source": "student_detection"}'
        db_session.commit()
        with acting_as(UserRole.instructor):
            client.post(f"/exercises/{ex.id}/run")
        obj = _obj(db_session, ex)
        assert (obj.achieved, obj.evidence) == (False, None)

    def test_a_query_that_does_not_parse_is_422_and_free(self, client, db_session):
        # M5: the Student's typo is not an outage, and costs no attempt.
        class Refuses(FakeStore):
            async def match(self, index, query, size=0):
                raise SearchQueryError("Failed to parse query [bytes_out:>1]")

        # In the detection grammar, but the store refuses it (e.g. a range on a keyword field).
        fastapi_app.dependency_overrides[search_backend] = lambda: Refuses()
        ex = _exercise(db_session)
        try:
            with acting_as(UserRole.student):
                resp = _submit(client, ex, "bytes_out:>1")
                assert resp.status_code == 422 and "Failed to parse" in resp.json()["detail"]
                for _ in range(19):
                    _submit(client, ex, "bytes_out:>1")
                assert _submit(client, ex, "bytes_out:>1").status_code == 429  # free, but not unlimited
        finally:
            fastapi_app.dependency_overrides.pop(search_backend, None)

    def test_no_search_backend_is_an_outage_not_a_miss(self, client, db_session):
        from app.search_backends import NullSearchBackend

        fastapi_app.dependency_overrides[search_backend] = lambda: NullSearchBackend()
        ex = _exercise(db_session)
        try:
            with acting_as(UserRole.student):
                assert _submit(client, ex, "a:b").status_code == 503
        finally:
            fastapi_app.dependency_overrides.pop(search_backend, None)

    def test_staff_detections_are_not_recorded_as_student_evidence(self, client, db_session, store):
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            assert _submit(client, ex, "url.domain:*northwind*").json()["verdict"] == "achieved"
        assert json.loads(_obj(db_session, ex).evidence)["source"] == "staff_detection"


class TestClosing:
    def test_complete_assesses_every_student_who_submitted(self, client, db_session, store, monkeypatch):
        # M7: auto-assessment, the LTI grade and xAPI go to the Students who took part.
        from app import celery_client
        from app.models import User

        dispatched = []
        monkeypatch.setattr(celery_client, "dispatch", lambda name, *args: dispatched.append((name, args)) or "id")
        store.events = [beacon(), beacon()]
        ex = _exercise(db_session)
        students = []
        for i in range(2):
            u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4().hex[:6]}", email=f"s{i}@example.org",
                     display_name=f"Student {i}", tenant_id=uuid.UUID(DEV_TENANT), role=UserRole.student)
            db_session.add(u)
            students.append(u)
        db_session.commit()
        for u in students:
            with acting_as(UserRole.student) as who:
                who.id = str(u.id)
                _submit(client, ex, "nope:x")
        with acting_as(UserRole.instructor) as instructor:
            assert client.post(f"/exercises/{ex.id}/complete").status_code == 200
        assessed = {args[1] for name, args in dispatched if name == "auto_assess_competency"}
        assert assessed == {str(students[0].id), str(students[1].id), instructor.id}

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
