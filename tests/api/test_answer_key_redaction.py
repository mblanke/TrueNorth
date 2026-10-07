"""Students get the briefing, not the answer key (ADR 0005 §5)."""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from _shared import DEV_TENANT, OTHER_TENANT, SCENARIO, acting_as
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, Scenario, UserRole

OLD_EVIDENCE = json.dumps({"source": "event_store", "index": "range-x", "query": "url.domain:*northwind*",
                           "threshold": 2, "events": [{"_id": "1"}]})
SCENARIO_WITH_PLAYBOOK = SCENARIO.replace(
    'timeline: [{t: "0:00"}]',
    'timeline:\n  - {t: "0:00", phase: 1, name: "C2 Establishment", action: http_burst, '
    'params: {url: "https://cdn.{{ c2_domain }}/x"}, mitre: T1071.001}',
)


@pytest.fixture
def exercise(db_session):
    sc = Scenario(id=uuid.uuid4(), name="sc", yaml=SCENARIO_WITH_PLAYBOOK, tenant_id=uuid.UUID(DEV_TENANT))
    ex = Exercise(id=uuid.uuid4(), name="ex", range_id=uuid.uuid4(), scenario_id=sc.id,
                  tenant_id=uuid.UUID(DEV_TENANT), state=ExerciseState.running, max_score=10, total_score=10)
    db_session.add_all([sc, ex, Objective(exercise_id=ex.id, ref_id="detect_c2", objective_type=ObjectiveType.detection,
                                          validator="opensearch_query", points=10, achieved=True, evidence=OLD_EVIDENCE)])
    db_session.commit()
    return ex


def _leaks(text: str) -> bool:
    return any(s in text for s in ("northwind", "url.domain", "http_burst", "c2_domain"))


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer])
def test_scenario_yaml_is_the_briefing_only(client, exercise, role):
    with acting_as(role):
        body = client.get(f"/scenarios/{exercise.scenario_id}").json()
    assert not _leaks(body["yaml"])
    doc = yaml.safe_load(body["yaml"])
    assert doc["timeline"] == [{"t": "0:00", "phase": 1, "name": "C2 Establishment"}]
    assert "variables" not in doc and all("params" not in o for o in doc["objectives"])
    assert [o["id"] for o in doc["objectives"]][:2] == ["detect_c2", "detect_phish"]


def test_instructor_sees_the_whole_scenario(client, exercise):
    with acting_as(UserRole.instructor):
        assert "url.domain" in client.get(f"/scenarios/{exercise.scenario_id}").json()["yaml"]


def test_scenario_detail_and_objectives_hide_the_key_from_students(client, exercise):
    with acting_as(UserRole.student):
        detail = client.get(f"/exercises/{exercise.id}/scenario-detail").json()
        objectives = client.get(f"/exercises/{exercise.id}/objectives").json()
    assert not _leaks(json.dumps(detail)) and not _leaks(json.dumps(objectives))
    assert json.loads(objectives[0]["evidence"]) == {"source": "event_store"}
    with acting_as(UserRole.instructor):
        assert _leaks(json.dumps(client.get(f"/exercises/{exercise.id}/scenario-detail").json()))


def test_objectives_of_another_tenant_are_not_found(client, exercise):
    with acting_as(UserRole.instructor, tenant=OTHER_TENANT):
        assert client.get(f"/exercises/{exercise.id}/objectives").status_code == 404


def test_unlisted_content_such_as_pre_staged_indicators_is_withheld():
    # Review B3: incident-response-drill lists the compromised hosts' indicators (the C2 IP
    # its detect objective keys on) under pre_staged_environment.
    from pathlib import Path

    from app.detections.redaction import redact_scenario_yaml

    root = Path(__file__).resolve().parents[2]
    text = (root / "content/scenarios/incident-response-drill/scenario.yaml").read_text()
    assert "10.60.200.10" in text
    briefing = redact_scenario_yaml(text)
    assert "10.60.200.10" not in briefing and "pre_staged_environment" not in briefing
    doc = yaml.safe_load(briefing)
    assert doc["timeline"][0]["tasks"]  # the Student's instructions stay


def test_every_shipped_scenario_briefing_leaks_no_objective_key():
    from pathlib import Path

    from app.detections.redaction import redact_scenario_yaml
    from app.detections.templating import render

    root = Path(__file__).resolve().parents[2]
    for path in sorted((root / "content/scenarios").glob("*/scenario.yaml")):
        doc = yaml.safe_load(path.read_text())
        briefing = redact_scenario_yaml(path.read_text())
        for obj in doc.get("objectives") or []:
            query = (obj.get("params") or {}).get("query")
            if isinstance(query, str):
                assert render(query, doc.get("variables")) not in briefing, (path.parent.name, obj["id"])
        for value in (doc.get("variables") or {}).values():
            assert str(value) not in briefing, (path.parent.name, value)
