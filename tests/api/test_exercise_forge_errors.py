"""Exercise Forge when the AI orchestrator misbehaves.

The orchestrator is replaced at the httpx boundary: down, timing out, failing,
or answering with something that is not a scenario. Each must map to a clear
5xx and persist nothing.
"""

from __future__ import annotations

import httpx
import pytest
from app.models import Exercise, Range, Scenario, Template
from app.routers import exercise_forge

DEV_TENANT = "00000000-0000-0000-0000-000000000001"

PAYLOAD = {
    "indicators": [{"indicator_type": "domain", "value": "evil.example", "severity": "high"}],
    "difficulty": "intermediate",
}


class _Orchestrator:
    """Stand-in for httpx.AsyncClient inside the forge router."""

    mode = "ok"
    output = "name: fine\nobjectives: []\n"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        request = httpx.Request("POST", url)
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "error":
            return httpx.Response(500, text="model exploded", request=request)
        return httpx.Response(200, json={"output": self.output, "model_used": "m"}, request=request)


@pytest.fixture
def orchestrator(monkeypatch):
    _Orchestrator.mode = "ok"
    _Orchestrator.output = "name: fine\nobjectives: []\n"
    monkeypatch.setattr(exercise_forge.httpx, "AsyncClient", _Orchestrator)
    return _Orchestrator


@pytest.fixture
def forge_range(db_session):
    tpl = Template(name="Forge TPL", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="Forge Range", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(rng)
    db_session.commit()
    return rng


@pytest.mark.parametrize(
    ("mode", "status", "detail"),
    [
        ("down", 503, "unavailable"),
        ("timeout", 503, "unavailable"),
        ("error", 502, "failed to generate"),
    ],
)
@pytest.mark.parametrize("endpoint", ["/exercise-forge/preview", "/exercise-forge/generate"])
def test_orchestrator_faults_map_to_5xx(client, db_session, orchestrator, forge_range, mode, status, detail, endpoint):
    orchestrator.mode = mode
    before = db_session.query(Scenario).count()
    r = client.post(endpoint, json=PAYLOAD)
    assert r.status_code == status
    assert detail in r.json()["detail"]
    assert db_session.query(Scenario).count() == before


@pytest.mark.parametrize("output", ["just a sentence", "- a\n- list\n", "key: [unclosed"])
def test_generate_rejects_output_that_is_not_a_scenario(client, db_session, orchestrator, forge_range, output):
    orchestrator.output = output
    before = db_session.query(Exercise).count()
    r = client.post("/exercise-forge/generate", json=PAYLOAD)
    assert r.status_code == 502
    assert db_session.query(Exercise).count() == before


def test_generate_without_a_range_is_409(client, orchestrator):
    r = client.post("/exercise-forge/generate", json=PAYLOAD)
    assert r.status_code == 409
