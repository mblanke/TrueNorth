"""A host installed with tn_ai_enabled: false, end to end in process: control-plane API ->
the real orchestrator app with what the installer renders (openai primary, OPENAI_BASE_URL and
OPENAI_API_KEY empty) -> a clear 503 "AI is not configured", never a 502 and never mock text.

The API's httpx client is wired straight into the orchestrator's ASGI app, as in
test_forge_through_orchestrator.py.
"""

from __future__ import annotations

import asyncio
import importlib

import httpx
import pytest
from app.models import Range, Template

orch = importlib.import_module("_ai_orch.main")

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def orchestrator_ai_off(monkeypatch):
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.openai)
    monkeypatch.setattr(orch, "OPENAI_BASE_URL", "")
    monkeypatch.setattr(orch, "OPENAI_API_KEY", "")
    monkeypatch.setattr(orch, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(orch, "FLEET", {})
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "")
    monkeypatch.setattr(orch, "_llm_semaphore", asyncio.Semaphore(2))

    def _client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.ASGITransport(app=orch.app), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ai/detection-draft", {"technique": "T1059.001", "data_source": "sysmon"}),
        ("/ai/scenario-draft", {"objectives": ["detect lateral movement"]}),
    ],
)
def test_ai_drafts_answer_503_ai_not_configured(client, orchestrator_ai_off, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 503, r.text
    assert r.json()["detail"].startswith("AI is not configured on this host")


def test_the_exercise_forge_answers_503_ai_not_configured(client, db_session, orchestrator_ai_off):
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
    assert r.status_code == 503, r.text
    assert r.json()["detail"].startswith("AI is not configured on this host")


def test_deep_health_says_ai_not_configured_and_stays_healthy(orchestrator_ai_off, monkeypatch):
    from app.health import HealthChecker, HealthStatus

    monkeypatch.setenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")
    comp = asyncio.run(HealthChecker()._check_ai_orchestrator())
    assert comp.status == HealthStatus.HEALTHY
    assert comp.details == {"ai_configured": False}
    assert comp.message == "HTTP 200; AI not configured"
