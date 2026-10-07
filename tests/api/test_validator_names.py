"""One spelling per validator (review finding 4)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from app.detections.names import CANONICAL, canonical_validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "scenario-engine/schemas/scenario.schema.json").read_text())


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("opensearch_query", "opensearch_query"),
        ("validate.opensearch_query", "opensearch_query"),
        ("validate.opensearch.query", "opensearch_query"),
        ("validate_opensearch_query", "opensearch_query"),
        ("validate.manual.ack", "manual_ack"),
        ("validate.deliverable_check", "deliverable_check"),
        ("", "manual_ack"),
        (None, "manual_ack"),
        ("custom.thing", "custom_thing"),
    ],
)
def test_canonical_validator(name, want):
    assert canonical_validator(name) == want


def test_schema_enumerates_the_canonical_names():
    enum = SCHEMA["properties"]["objectives"]["items"]["properties"]["validator"]["enum"]
    assert tuple(enum) == CANONICAL


@pytest.mark.parametrize("path", sorted((ROOT / "content/scenarios").glob("*/scenario.yaml")), ids=lambda p: p.parent.name)
def test_shipped_scenarios_use_canonical_validator_names(path):
    doc = yaml.safe_load(path.read_text())
    names = [o.get("validator") for o in doc.get("objectives") or []]
    assert all(n in CANONICAL for n in names), names


def test_schema_rejects_a_dotted_spelling():
    bad = {"objectives": [{"id": "o", "type": "detection", "validator": "validate.opensearch.query", "points": 1}]}
    sub = {"type": "object", "properties": {"objectives": SCHEMA["properties"]["objectives"]}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, sub)
