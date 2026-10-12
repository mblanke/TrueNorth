"""Scenario validation refuses objectives that cannot be judged honestly (R0-B).

``POST /scenarios/validate`` (app/engine_bridge.validate_yaml) runs the engine's JSON
Schema and ``scenario_engine.objective_catalogue.objective_errors``. incident-response-drill
named ``network_isolation``, ``firewall_rule`` and ``artifact_removal`` in phase-level
``validators:`` blocks that nothing evaluated, and ``firewall_rule`` passed every objective.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from app import engine_bridge
from app.detections.names import CANONICAL
from scenario_engine.objective_catalogue import REFUSED_VALIDATORS, SCENARIO_VALIDATORS, objective_errors
from scenario_engine.scoring.validators import ScoringValidator

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = sorted([*(ROOT / "content/scenarios").glob("**/scenario.yaml"), *(ROOT / "scenario-engine/examples").glob("*.yaml")])
HEAD = "name: s\nversion: '1'\nrange_template: t\ntimeline: [{t: '0:00'}]\n"


def _doc(objective: str, head: str = HEAD) -> str:
    return head + "objectives:\n  - " + objective + "\n"


def _errors(text: str) -> list[dict]:
    result = engine_bridge.validate_yaml("scenario", text)
    assert result["valid"] is (not result["errors"])
    return result["errors"]


def test_every_shipped_scenario_validates():
    assert SCENARIOS, "no shipped scenarios found"
    failures = {
        p.relative_to(ROOT).as_posix(): errs
        for p in SCENARIOS
        if (errs := engine_bridge.validate_yaml("scenario", p.read_text(encoding="utf-8"))["errors"])
    }
    assert failures == {}


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_shipped_scenarios_name_only_judged_validators(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert objective_errors(doc) == []
    assert all("validators" not in phase for phase in doc.get("timeline") or [])


@pytest.mark.parametrize("name", sorted(REFUSED_VALIDATORS))
def test_a_refused_validator_is_rejected_with_its_reason(name):
    errors = _errors(_doc(f"{{id: o, type: response, validator: {name}, points: 5}}"))
    refusal = [e for e in errors if "is refused" in e["message"]]
    assert refusal and refusal[0]["path"] == "objectives.0.validator"
    assert REFUSED_VALIDATORS[name] in refusal[0]["message"]


@pytest.mark.parametrize("name", ["network_isolation", "artifact_removal", "custom_thing"])
def test_an_unknown_validator_is_rejected(name):
    errors = _errors(_doc(f"{{id: o, type: response, validator: {name}, points: 5}}"))
    assert any(e["path"] == "objectives.0.validator" and f"unknown validator '{name}'" in e["message"] for e in errors)


def test_phase_level_validators_are_rejected():
    head = (
        "name: s\nversion: '1'\nrange_template: t\n"
        "timeline:\n  - t: '0:30'\n    validators:\n      - {type: network_isolation, hosts: [a]}\n"
        "      - {type: firewall_rule, block_ip: 10.0.0.1}\n"
    )
    errors = _errors(_doc("{id: o, type: response, validator: manual_ack, points: 5}", head))
    paths = {e["path"]: e["message"] for e in errors}
    assert set(paths) == {"timeline.0.validators.0", "timeline.0.validators.1"}
    assert "never evaluated" in paths["timeline.0.validators.0"]
    assert "'network_isolation' is not a validator" in paths["timeline.0.validators.0"]
    assert "manual_ack" in paths["timeline.0.validators.1"]


def test_a_manual_objective_cannot_approve_itself():
    errors = _errors(_doc("{id: o, type: response, validator: manual_ack, points: 5, params: {approved: true}}"))
    assert [e["path"] for e in errors] == ["objectives.0.params.approved"]


def test_user_disabled_needs_a_plain_account_name():
    assert _errors(_doc("{id: o, type: response, validator: user_disabled, points: 5}"))
    assert _errors(_doc("{id: o, type: response, validator: user_disabled, points: 5, params: {username: 'a OR b'}}"))
    templated = "{id: o, type: response, validator: user_disabled, points: 5, params: {username: '{{ u }}'}}"
    assert _errors(_doc(templated))  # undefined variable
    ok = _doc(templated, HEAD + "variables: {u: svc_backup}\n")
    assert _errors(ok) == []


def test_require_observed_must_be_a_boolean():
    bad = "{id: o, type: response, validator: opensearch_query, points: 5, params: {query: 'a:b', require_observed: 1}}"
    assert [e["path"] for e in _errors(_doc(bad))] == ["objectives.0.params.require_observed"]
    assert _errors(_doc(bad.replace("require_observed: 1", "require_observed: true"))) == []


def test_catalogue_schema_and_api_names_agree():
    schema = json.loads((ROOT / "scenario-engine/schemas/scenario.schema.json").read_text())
    enum = schema["properties"]["objectives"]["items"]["properties"]["validator"]["enum"]
    assert set(enum) == set(SCENARIO_VALIDATORS) == set(CANONICAL)
    assert not set(REFUSED_VALIDATORS) & set(SCENARIO_VALIDATORS)


def test_no_scoring_method_left_for_a_refused_validator():
    for name in REFUSED_VALIDATORS:
        assert not hasattr(ScoringValidator, f"validate_{name}"), name
