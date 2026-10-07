"""Templated objective queries are rendered from ``variables:`` or rejected (review finding 3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from app import engine_bridge
from app.detections import templating

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scenario-engine"))
from scenario_engine import variables as engine_variables  # noqa: E402

SCENARIOS = sorted((ROOT / "content/scenarios").glob("*/scenario.yaml"))


def test_api_renderer_mirrors_the_engine():
    assert Path(templating.__file__).read_text().split('"""', 2)[2] == Path(engine_variables.__file__).read_text().split('"""', 2)[2]


def test_render_fills_known_and_keeps_unknown():
    q = "url.domain:*{{ c2_domain }}* AND host:{{host}}"
    out = templating.render(q, {"c2_domain": "x.example"})
    assert out == "url.domain:*x.example* AND host:{{host}}"
    assert templating.unresolved(out) == ["host"]


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.parent.name)
def test_every_shipped_objective_query_renders_fully(path):
    doc = yaml.safe_load(path.read_text())
    for obj in doc.get("objectives") or []:
        query = (obj.get("params") or {}).get("query")
        if isinstance(query, str):
            assert templating.unresolved(templating.render(query, doc.get("variables"))) == [], obj["id"]


def test_validate_rejects_an_undefined_query_variable():
    doc = (
        "name: s\nversion: '1'\nrange_template: t\ntimeline: [{t: '0:00'}]\n"
        "objectives:\n  - {id: o, type: detection, validator: opensearch_query, points: 1,\n"
        "     params: {query: 'url.domain:{{ c2_domain }}'}}\n"
    )
    result = engine_bridge.validate_yaml("scenario", doc)
    assert not result["valid"]
    assert result["errors"] == [
        {"path": "objectives.0.params.query", "message": "undefined variable(s) c2_domain: add them under variables:"}
    ]
    ok = engine_bridge.validate_yaml("scenario", doc.replace("objectives:", "variables: {c2_domain: a.example}\nobjectives:"))
    assert ok["valid"], ok["errors"]


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.parent.name)
def test_shipped_scenarios_have_no_unrendered_query_errors(path):
    errors = engine_bridge.validate_yaml("scenario", path.read_text())["errors"]
    assert [e for e in errors if e["path"].endswith("params.query")] == []
