# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""The scenario a Student sees is the briefing, never the answer key (ADR 0005 §5).

app/detections/redaction.py allows only briefing keys through: an objective's ``params``
(the ground-truth queries), the scenario ``variables:`` they render from and the timeline's
``action``/``params`` (the inject playbook) are withheld, and a key the allowlist does not
name stays hidden. A document that does not parse as YAML is withheld entirely, and older
scorer evidence loses the query and matched events it used to carry.
"""

from __future__ import annotations

import json

import pytest
from app.auth import CurrentUser
from app.detections.redaction import (
    BRIEFING_KEYS,
    EVIDENCE_KEY_FIELDS,
    OBJECTIVE_KEYS,
    TIMELINE_KEYS,
    redact_evidence,
    redact_scenario,
    redact_scenario_yaml,
    redact_timeline,
    sees_answer_key,
)
from app.models import UserRole

SCENARIO_DOC = {
    "name": "Northwind Update",
    "description": "a phishing run",
    "variables": {"c2_domain": "northwind-update.example"},  # answer key: withheld
    "timeline": [
        {
            "t": "0:05",
            "name": "beacon",
            "description": "a beacon starts",
            "action": "c2_beacon",
            "params": {"mitre_technique": "T1071"},
        },
        "not a step",  # a malformed step is dropped, not passed through
    ],
    "objectives": [
        {
            "id": "detect_c2",
            "type": "detection",
            "name": "find the beacon",
            "points": 40,
            "params": {"query": "url.domain:*northwind*"},
        },  # the look-for: withheld
        {"also hidden": True},  # a step with none of the briefing keys yields nothing
    ],
    "pre_staged_environment": {"compromised": ["dc01"]},  # never briefing
}


def _who(role: UserRole) -> CurrentUser:
    return CurrentUser(id="u", email="u@example.test", display_name="u", role=role, tenant_id="t", keycloak_id="kc")


def test_staff_see_the_answer_key_students_do_not():
    assert sees_answer_key(_who(UserRole.instructor)) is True
    assert sees_answer_key(_who(UserRole.student)) is False


def test_the_briefing_keeps_only_the_allowlist():
    out = redact_scenario(SCENARIO_DOC)
    assert set(out) <= set(BRIEFING_KEYS) | {"timeline", "objectives"}
    assert out["name"] == "Northwind Update"
    assert "variables" not in out and "pre_staged_environment" not in out


def test_the_timeline_is_redacted_to_what_happens_when():
    (step,) = redact_timeline(SCENARIO_DOC["timeline"])
    assert set(step) <= set(TIMELINE_KEYS)
    assert step["t"] == "0:05" and step["name"] == "beacon"
    assert "action" not in step and "params" not in step


def test_a_timeline_that_is_not_a_list_of_steps_redacts_to_empty():
    assert redact_timeline(None) == []
    assert redact_timeline([]) == []
    assert redact_timeline(["a string", 7]) == []


def test_objectives_keep_their_shape_but_lose_the_params():
    named, anonymous = redact_scenario(SCENARIO_DOC)["objectives"]
    assert set(named) <= set(OBJECTIVE_KEYS)
    assert named["id"] == "detect_c2" and named["points"] == 40
    assert "params" not in named
    # An objective none of whose keys are briefing keys redacts to an empty mapping
    # (it is filtered out by kind, not by content).
    assert anonymous == {}


def test_the_yaml_briefing_is_rendered_without_the_key():
    import yaml

    out = redact_scenario_yaml(yaml.safe_dump(SCENARIO_DOC))
    doc = yaml.safe_load(out)
    assert "variables" not in doc and "params" not in doc["objectives"][0]
    assert doc["timeline"][0]["name"] == "beacon"


@pytest.mark.parametrize("text", [None, "", "not: [valid: yaml", "- a\n- list\n", "42"])
def test_a_document_that_is_not_a_mapping_is_withheld_entirely(text):
    assert redact_scenario_yaml(text) == ""


def test_old_scorer_evidence_loses_its_key_fields():
    evidence = json.dumps(
        {"query": "url.domain:*northwind*", "events": [1, 2], "threshold": 2, "min_precision": 0.5, "events_matched": 2}
    )
    out = json.loads(redact_evidence(evidence))
    assert set(out) == {"min_precision", "events_matched"}  # every key field is gone
    assert not set(out) & set(EVIDENCE_KEY_FIELDS)


def test_evidence_that_is_not_a_json_object_is_returned_untouched():
    assert redact_evidence("not json at all") == "not json at all"
    assert redact_evidence("[1, 2, 3]") == "[1, 2, 3]"
    assert redact_evidence("") == ""


def test_no_evidence_is_no_evidence():
    assert redact_evidence(None) is None
