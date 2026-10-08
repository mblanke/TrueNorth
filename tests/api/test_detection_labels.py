"""Students cannot earn detection credit by querying the platform's inject labels (H4).

Inject telemetry was stamped with ``inject_action``, ``exercise_id``, ``truenorth_simulated``,
``event.module: truenorth.inject`` and ``threat.technique.id``, and a Student's detection
ran as raw Lucene ``query_string``. ``inject_action:c2_beacon`` matched exactly the attack,
at full precision, and was credited. Now: Student queries go through the closed detection
grammar (labels and free text refused, 422, no attempt used), the labels live under
``tn_ground_truth`` (stored, not indexed), and telemetry search hides them from non-staff.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from _shared import SCENARIO, STARTED, FakeStore, _at, act_as, real_exercise, real_tenant, real_user
from app import main as app_main
from app.detections import credit
from app.detections.models import DetectionSubmission
from app.main import app as fastapi_app
from app.models import ExerciseState, Objective, ObjectiveType, UserRole
from app.routers.detections import search_backend
from app.search_backends import detection_query
from app.search_backends.query import QueryError

REPO = Path(__file__).resolve().parents[2]


def _labelled(minutes=5, **labels):
    """An attack event as inject telemetry used to carry it: observables plus flat labels."""
    return {"event_type": "http", "url.domain": "cdn.northwind-update.example",
            "truenorth": {"ingested_at": _at(minutes)}, **labels}


@pytest.fixture
def store():
    s = FakeStore()
    fastapi_app.dependency_overrides[search_backend] = lambda: s
    yield s
    fastapi_app.dependency_overrides.pop(search_backend, None)


@pytest.fixture
def setting(db_session):
    a, b = real_tenant(db_session, "det-a"), real_tenant(db_session, "det-b")
    ex = real_exercise(db_session, a.id, SCENARIO, state=ExerciseState.running, started_at=STARTED,
                       max_score=40, total_score=0)
    db_session.add(Objective(exercise_id=ex.id, ref_id="detect_c2", objective_type=ObjectiveType.detection,
                             description="c2", validator="opensearch_query", points=40))
    db_session.flush()
    return {"ex": ex, "student": real_user(db_session, UserRole.student, a.id),
            "other_tenant_student": real_user(db_session, UserRole.student, b.id)}


EXPLOITS = [
    "inject_action:c2_beacon",
    "exercise_id:*",
    "truenorth_simulated:true",
    "event.module:truenorth.inject",
    "event.module:truenorth*",
    "threat.technique.id:T1071*",
    "inject_target:*",
    "tn_ground_truth.inject_action:c2_beacon",
    "url.domain:*northwind* AND inject_action:c2_beacon",
    "url.domain:x OR exercise_id:*",
    "c2_beacon",  # free text searched every field, labels included
]


@pytest.mark.parametrize("query", EXPLOITS)
def test_label_queries_are_422_use_no_attempt_and_earn_nothing(client, db_session, store, setting, query):
    store.events = [_labelled(inject_action="c2_beacon", exercise_id=str(setting["ex"].id),
                              truenorth_simulated=True, **{"event.module": "truenorth.inject",
                                                           "threat.technique.id": "T1071.001"}) for _ in range(3)]
    act_as(setting["student"])

    r = client.post(f"/exercises/{setting['ex'].id}/objectives/detect_c2/detections", json={"query": query})

    assert r.status_code == 422, r.text
    assert "no attempt used" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.query(DetectionSubmission).count() == 0
    obj = db_session.query(Objective).filter(Objective.exercise_id == setting["ex"].id).one()
    assert obj.achieved is False


def test_a_real_detection_on_observable_fields_still_scores(client, db_session, store, setting):
    store.events = [_labelled(), _labelled(), {"event_type": "http", "url.domain": "news.example",
                                              "truenorth": {"ingested_at": _at(5)}}]
    act_as(setting["student"])
    r = client.post(f"/exercises/{setting['ex'].id}/objectives/detect_c2/detections",
                    json={"query": "event_type:http AND url.domain:*northwind-update*"})
    assert r.status_code == 201, r.text
    assert r.json()["verdict"] == "achieved"


def test_every_shipped_answer_key_is_expressible_in_the_detection_grammar():
    """A Student must be able to write what the key says, or the objective is unwinnable."""
    keys = []
    for path in sorted((REPO / "content/scenarios").glob("*/scenario.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for obj in doc.get("objectives") or []:
            q = (obj.get("params") or {}).get("query")
            if isinstance(q, str):
                keys.append((path.parent.name, obj.get("id"), credit.render(q, doc.get("variables") or {})))
    assert keys
    for scenario, ref, q in keys:
        try:
            detection_query.parse_detection(q)
        except QueryError as exc:  # pragma: no cover - the message is the point
            pytest.fail(f"{scenario}:{ref} key {q!r} is not expressible: {exc}")
        assert not detection_query.references_labels(q), (scenario, ref)


def test_an_answer_key_on_labels_is_not_scorable():
    doc = "objectives:\n  - id: o\n    params: {query: 'inject_action:c2_beacon'}\n"
    with pytest.raises(credit.NotScorable, match="labels"):
        credit.answer_key("opensearch_query", None, "o", doc)


def test_the_label_namespace_is_the_one_the_engine_writes():
    from scenario_engine.injectors import GROUND_TRUTH_FIELD

    assert detection_query.GROUND_TRUTH_FIELD == GROUND_TRUTH_FIELD


def test_the_range_template_does_not_index_the_labels():
    import importlib
    import sys

    sys.path.insert(0, str(REPO / "telemetry"))
    try:
        bootstrap = importlib.import_module("pipelines.bootstrap")
    finally:
        sys.path.pop(0)
    props = bootstrap.RANGE_TEMPLATE["template"]["mappings"]["properties"]
    assert props[detection_query.GROUND_TRUTH_FIELD] == {"type": "object", "enabled": False}


# ── telemetry search: non-staff neither query nor see the labels ───────
class _Backend:
    async def search(self, index, query, size):
        return {"hits": {"hits": [{"_source": {
            "url.domain": "x.example", "inject_action": "c2_beacon", "exercise_id": "e",
            "event.module": "truenorth.inject", "tn_ground_truth": {"inject_action": "c2_beacon"},
            "event": {"module": "truenorth.inject", "kind": "event"}}}]}}


@pytest.fixture
def search(monkeypatch, db_session):
    monkeypatch.setattr(app_main, "get_search_backend", lambda: _Backend())
    t = real_tenant(db_session, "search-a")
    ex = real_exercise(db_session, t.id)
    return t, ex.range_id


def test_an_observer_cannot_query_labels_and_does_not_see_them(client, db_session, search):
    tenant, range_id = search
    act_as(real_user(db_session, UserRole.observer, tenant.id))

    assert client.get(f"/telemetry/{range_id}/search", params={"q": "inject_action:c2_beacon"}).status_code == 422
    r = client.get(f"/telemetry/{range_id}/search", params={"q": "*"})
    assert r.status_code == 200, r.text
    src = r.json()["hits"]["hits"][0]["_source"]
    assert src == {"url.domain": "x.example", "event": {"kind": "event"}}


def test_instructors_still_see_the_labels(client, db_session, search):
    tenant, range_id = search
    act_as(real_user(db_session, UserRole.instructor, tenant.id))
    src = client.get(f"/telemetry/{range_id}/search", params={"q": "*"}).json()["hits"]["hits"][0]["_source"]
    assert src["tn_ground_truth"] == {"inject_action": "c2_beacon"}


def test_no_cross_tenant_side_channel(client, db_session, store, setting):
    act_as(setting["other_tenant_student"])
    r = client.post(f"/exercises/{setting['ex'].id}/objectives/detect_c2/detections", json={"query": "a:b"})
    assert r.status_code == 404
    assert uuid.UUID(str(setting["ex"].id))  # unchanged
