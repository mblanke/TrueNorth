"""Control plane -> real orchestrator app (mock backend) -> persisted exercise, in process.

The integration lane proves the same chain over the network
(tests/integration/test_ai_forge_flow.py); this one runs everywhere, wiring the
forge router's httpx client straight into the orchestrator's ASGI app.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest
import respx
from app.models import Exercise, ForgedExercise, Objective, Range, Template
from app.routers import exercise_forge

orch = importlib.import_module("_ai_orch.main")
orch_backends = importlib.import_module("_ai_orch.backends")

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def orchestrator_in_process(monkeypatch):
    orch_backends._reset_backends()
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.mock)
    monkeypatch.setattr(orch, "_llm_semaphore", asyncio.Semaphore(2))

    def _client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.ASGITransport(app=orch.app), **kwargs)

    monkeypatch.setattr(exercise_forge.httpx, "AsyncClient", _client)
    yield
    orch_backends._reset_backends()


def test_generate_persists_the_mock_scenario(client, db_session, orchestrator_in_process):
    tpl = Template(name="TPL", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="R", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(rng)
    db_session.commit()

    r = client.post(
        "/exercise-forge/generate",
        json={
            "indicators": [{"indicator_type": "domain", "value": "evil.example", "mitre_attack_ids": ["T1566.001"]}],
            "range_id": str(rng.id),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_used"] == "mock"
    assert body["name"] == "mock-forged-phishing-intrusion"
    assert {"T1566.001", "T1059.001"} <= set(body["mitre_techniques"])

    ex = db_session.get(Exercise, body["exercise_id"])
    assert ex is not None and str(ex.range_id) == str(rng.id)
    objectives = db_session.query(Objective).filter(Objective.exercise_id == ex.id).all()
    assert sorted(o.ref_id for o in objectives) == ["contain-host", "detect-phish"]
    assert sum(o.points for o in objectives) == 100
    assert db_session.query(ForgedExercise).filter(ForgedExercise.exercise_id == ex.id).count() == 1


@respx.mock
def test_model_backend_down_surfaces_as_502(client, orchestrator_in_process, monkeypatch):
    # Orchestrator is up, its provider is not: the orchestrator answers 503 and the
    # control plane reports "failed to generate" rather than crashing.
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.anthropic)
    monkeypatch.setattr(orch, "ANTHROPIC_API_KEY", "k")
    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=httpx.ConnectError("down"))
    respx.route(host="ai-orchestrator").pass_through()
    r = client.post("/exercise-forge/preview", json={"learning_objectives": ["Detect phishing"]})
    assert r.status_code == 502
    assert "failed to generate" in r.json()["detail"]


def test_the_control_plane_presents_the_service_token(client, orchestrator_in_process, monkeypatch):
    # The orchestrator demands AI_SERVICE_TOKEN; the API sends the same value it is given.
    token = "shared-service-token-0123456789abcdefghij"
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", token)
    body = {"learning_objectives": ["Detect phishing"]}
    monkeypatch.delenv("AI_SERVICE_TOKEN", raising=False)
    assert client.post("/exercise-forge/preview", json=body).status_code == 502  # refused: 401 upstream
    monkeypatch.setenv("AI_SERVICE_TOKEN", token)
    r = client.post("/exercise-forge/preview", json=body)
    assert r.status_code == 200, r.text


def test_every_orchestrator_caller_sends_the_token():
    """Each API/worker module that talks to AI_ORCHESTRATOR_URL passes orchestrator_headers()."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "control-plane"
    missing = []
    for path in [*root.glob("api/app/**/*.py"), *root.glob("worker/worker/**/*.py")]:
        text = path.read_text()
        if path.name in ("health.py", "base.py"):  # GET /health is public; base.py only names it
            continue
        if "AI_ORCHESTRATOR_URL" in text and "orchestrator_headers" not in text:
            missing.append(str(path.relative_to(root)))
    assert not missing, missing
