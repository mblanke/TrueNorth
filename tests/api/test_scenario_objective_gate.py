"""Scenario create/update refuse objectives that could never be judged honestly (R0-B).

``POST /scenarios/validate`` reported them, but create and update only parsed the YAML, so
a scenario naming ``firewall_rule`` (which passed every objective) or a ``manual_ack`` that
approved itself could be stored and launched. Also: a ``require_observed`` objective is
never credited from a Student's submitted detection (detections/credit.py).
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app.detections import credit
from app.models import Scenario, UserRole

HEAD = "name: s\nversion: '1'\nrange_template: t\ntimeline: [{t: '0:00'}]\nobjectives:\n  - "
GOOD = HEAD + "{id: o, type: response, validator: manual_ack, points: 5}\n"


@pytest.mark.parametrize(
    ("objective", "path"),
    [
        ("{id: o, type: response, validator: firewall_rule, points: 5}", "objectives.0.validator"),
        ("{id: o, type: response, validator: network_isolation, points: 5}", "objectives.0.validator"),
        ("{id: o, type: response, validator: manual_ack, points: 5, params: {approved: true}}", "objectives.0.params.approved"),
        ("{id: o, type: deliverable, validator: deliverable_check, points: 5, params: {storage_path: /etc/hosts}}",
         "objectives.0.params.storage_path"),
    ],
    ids=["refused", "unknown", "self-approved", "content-file"],
)
def test_create_and_update_refuse_unjudgeable_objectives(client, db_session, objective, path):
    t = real_tenant(db_session, f"gate-{uuid.uuid4().hex[:6]}")
    act_as(real_user(db_session, UserRole.admin, t.id))
    bad = HEAD + objective + "\n"

    r = client.post("/scenarios", json={"name": "bad", "version": "1", "yaml": bad})
    assert r.status_code == 422, r.text
    assert path in [e["path"] for e in r.json()["detail"]["errors"]]
    assert db_session.query(Scenario).filter(Scenario.name == "bad", Scenario.tenant_id == t.id).count() == 0

    ok = client.post("/scenarios", json={"name": "good", "version": "1", "yaml": GOOD})
    assert ok.status_code == 201, ok.text
    sid = ok.json()["id"]
    r = client.put(f"/scenarios/{sid}", json={"yaml": bad})
    assert r.status_code == 422, r.text
    assert db_session.get(Scenario, uuid.UUID(sid)).yaml == GOOD

    # A rename carries no yaml and is not re-checked.
    assert client.put(f"/scenarios/{sid}", json={"name": "renamed"}).status_code == 200


def test_validate_and_create_agree(client, db_session):
    t = real_tenant(db_session, "gate-agree")
    act_as(real_user(db_session, UserRole.admin, t.id))
    bad = HEAD + "{id: o, type: response, validator: process_killed, points: 5}\n"
    validated = client.post("/scenarios/validate", json={"yaml": bad}).json()
    created = client.post("/scenarios", json={"name": "x", "version": "1", "yaml": bad}).json()
    refused = [e for e in validated["errors"] if "is refused" in e["message"]]
    assert refused == created["detail"]["errors"]


@pytest.mark.parametrize("crit", [[], ["Lateral movement via PsExec"], ["Phishing email opened", "Credential dump"]])
def test_qsp_scenarios_have_only_judgeable_objectives(crit):
    # qsp_paths stores its generated scenario directly; it only ever writes fixed canonical
    # validators with no params, which this holds to the same objective check.
    from types import SimpleNamespace

    from app import engine_bridge, qsp_paths, safe_yaml

    po = SimpleNamespace(po_code="PO-1", title="Respond", environment=SimpleNamespace(value="it"),
                         duration_min=60, deliverable="technical report")
    assert engine_bridge.objective_errors(safe_yaml.load(qsp_paths._scenario_yaml(po, crit))) == []


def _key(params: str):
    doc = f"objectives:\n  - id: o\n    params: {params}\n"
    return credit.answer_key("opensearch_query", None, "o", doc)


@pytest.mark.parametrize("flag", ["true", "'yes'", "1"])
def test_an_observed_objective_is_not_credited_from_a_submission(flag):
    # The answer key would match the inject's synthetic records too (their label is not
    # indexed), so a submission against it is never credited; the engine judges it.
    with pytest.raises(credit.NotScorable, match="observed range telemetry"):
        _key(f"{{query: 'event_type:flow', require_observed: {flag}}}")


def test_a_detection_objective_is_still_credited_from_submissions():
    assert _key("{query: 'event_type:flow', require_observed: false}").query == "event_type:flow"
    assert _key("{query: 'event_type:flow'}").query == "event_type:flow"
