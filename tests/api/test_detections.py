"""Detection credit comes from what the Student did (ADR 0005, review finding 1).

The scenario query is the answer key. An idle Student scores nothing; a Student's query
is credited only when it finds enough of the attack, precisely enough, inside the
exercise window measured on the server's ingest clock.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.detections.models import DetectionSubmission
from app.main import app as fastapi_app
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, Scenario, UserRole
from app.rbac import ROLE_PERMISSIONS, Permission
from app.routers.detections import search_backend
from app.search_backends import BaseSearchBackend, SearchBackendError, SearchMatch

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
STARTED = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

SCENARIO = """
name: s
version: "1"
range_template: t
variables:
  c2_domain: northwind-update.example
timeline: [{t: "0:00"}]
objectives:
  - id: detect_c2
    type: detection
    validator: opensearch_query
    params: {query: 'event_type:http AND url.domain:*{{ c2_domain }}*', min_hits: 2}
    points: 40
  - id: detect_phish
    type: detection
    validator: validate.opensearch_query
    params: {query: 'event_type:email AND attachment.name:*.docm'}
    points: 10
  - id: undefined_var
    type: detection
    validator: opensearch_query
    params: {query: 'src:{{ attacker_ip }}'}
    points: 5
  - id: write_report
    type: deliverable
    validator: deliverable_check
    points: 5
"""

_TERM = re.compile(r'([\w.@]+):("([^"]*)"|\S+)')


def _get(event: dict, dotted: str):
    if dotted in event:
        return event[dotted]
    cur = event
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class FakeStore(BaseSearchBackend):
    """Understands what credit.judge sends: bool.filter of query_string (field:value AND
    terms, * wildcards, or ``*`` alone) and a range on truenorth.ingested_at."""

    def __init__(self, events=(), down=False):
        self.events = list(events)
        self.down = down
        self.indices: set[str] = set()

    def _matches(self, event, clause) -> bool:
        if "query_string" in clause:
            q = clause["query_string"]["query"].strip()
            if q == "*":
                return True
            terms = [(f, inner if raw.startswith('"') else raw) for f, raw, inner in _TERM.findall(q)]
            return bool(terms) and all(fnmatch.fnmatch(str(_get(event, f)), pat) for f, pat in terms)
        if "range" in clause:
            [(field, bounds)] = clause["range"].items()
            value = _get(event, field)
            return value is not None and bounds["gte"] <= value <= bounds["lte"]
        raise AssertionError(clause)

    async def match(self, index, query, size=0):
        if self.down:
            raise SearchBackendError("connection refused")
        self.indices.add(index)
        hits = [i for i, e in enumerate(self.events) if all(self._matches(e, c) for c in query["bool"]["filter"])]
        return SearchMatch(total=len(hits), ids=[str(i) for i in hits[:size]])

    async def ingest(self, index, events):
        return 0

    async def search(self, index, query, size=50):
        return {}

    async def health_check(self):
        return True


def _at(minutes: float) -> str:
    return (STARTED + timedelta(minutes=minutes)).isoformat()


def beacon(minutes=5):
    return {"event_type": "http", "url.domain": "cdn.northwind-update.example", "truenorth": {"ingested_at": _at(minutes)}}


def noise(minutes=5, n=1):
    return [{"event_type": "http", "url.domain": f"site{i}.example", "truenorth": {"ingested_at": _at(minutes)}}
            for i in range(n)]


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=f"Test {role.value}",
                      role=role, tenant_id=tenant, keycloak_id="kc")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


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
        assert store.indices == {f"range-{ex.range_id}"}

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
