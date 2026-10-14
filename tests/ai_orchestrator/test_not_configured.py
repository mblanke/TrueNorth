"""With no AI endpoint configured the orchestrator says so instead of calling out.

OLLAMA_PROXY_URL used to default to one person's internet-facing host. It was never
read, but it advertised a default endpoint; it is gone. The orchestrator has no default
AI endpoint: Ollama nodes come only from OLLAMA_NODES, cloud backends only from keys.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

orch = importlib.import_module("_ai_orch.main")


@pytest.fixture
def bare(monkeypatch):
    """Nothing configured: Ollama primary, empty fleet, no provider keys, open auth."""
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.ollama)
    monkeypatch.setattr(orch, "FLEET", {})
    monkeypatch.setattr(orch, "OPENAI_API_KEY", "")
    monkeypatch.setattr(orch, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "")
    return TestClient(orch.app)  # no lifespan: no health loop, no node clients


def test_no_default_external_endpoint():
    assert not hasattr(orch, "OLLAMA_PROXY_URL")


def test_empty_fleet_env_means_no_nodes(monkeypatch):
    monkeypatch.delenv("OLLAMA_NODES", raising=False)
    assert orch._parse_fleet() == {}


def test_health_reports_ai_not_configured(bare):
    r = bare.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ai_configured"] is False
    assert body["fleet"] == {}


@pytest.mark.parametrize(
    ("primary", "attr", "value"),
    [
        ("ollama", "FLEET", {"n1": None}),
        ("ollama", "OPENAI_API_KEY", "sk-x"),
        ("openai", "OPENAI_API_KEY", "sk-x"),
        ("anthropic", "ANTHROPIC_API_KEY", "sk-ant"),
    ],
)
def test_a_configured_backend_is_reported(bare, monkeypatch, primary, attr, value):
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType(primary))
    monkeypatch.setattr(orch, attr, value)
    assert orch.ai_configured() is True


@pytest.mark.parametrize("primary", ["openai", "anthropic", "mock"])
def test_a_primary_without_its_key_is_not_configured(bare, monkeypatch, primary):
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType(primary))
    assert orch.ai_configured() is False


# ── Generation without a backend: 503, never mock text (tn_ai_enabled: false) ──


@pytest.fixture
def ai_off(bare, monkeypatch):
    """What the installer renders with tn_ai_enabled: false: openai primary, empty URL and key."""
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.openai)
    monkeypatch.setattr(orch, "OPENAI_BASE_URL", "")
    monkeypatch.setattr(orch, "_llm_semaphore", asyncio.Semaphore(2))
    return bare


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ai/detection-rule", {"technique": "T1059.001"}),
        ("/ai/scenario-suggest", {"objectives": ["detect lateral movement"]}),
        ("/ai/generate", {"task": "general", "prompt": "hello"}),
    ],
)
def test_generation_without_a_backend_is_a_clear_503(ai_off, path, body):
    r = ai_off.post(path, json=body)
    assert r.status_code == 503, r.text
    assert r.headers[orch.AI_NOT_CONFIGURED_HEADER] == "true"
    assert r.json()["detail"].startswith("AI is not configured on this host")
    assert "MOCK" not in r.text
    assert ai_off.get("/health").json()["ai_configured"] is False


def test_an_empty_endpoint_is_not_configured_even_with_a_key(ai_off, monkeypatch):
    monkeypatch.setattr(orch, "OPENAI_API_KEY", "sk-left-in-the-vault")
    assert orch.ai_configured() is False
    assert ai_off.post("/ai/detection-rule", json={"technique": "T1059.001"}).status_code == 503


def test_an_unset_endpoint_with_a_key_is_configured(ai_off, monkeypatch):
    """Unset (not empty) keeps the backend's own default URL, as before."""
    monkeypatch.setattr(orch, "OPENAI_BASE_URL", None)
    monkeypatch.setattr(orch, "OPENAI_API_KEY", "sk-x")
    assert orch.ai_configured() is True


def test_an_explicit_mock_primary_still_answers(ai_off, monkeypatch):
    """CI and the integration stack run AI_MODEL_BACKEND=mock on purpose."""
    monkeypatch.setattr(orch, "PRIMARY_BACKEND", orch.BackendType.mock)
    r = ai_off.post("/ai/detection-rule", json={"technique": "T1059.001"})
    assert r.status_code == 200, r.text
    assert orch.AI_NOT_CONFIGURED_HEADER not in r.headers


def test_embedding_without_an_endpoint_is_503_not_500(ai_off, monkeypatch):
    backends = importlib.import_module("_ai_orch.backends")
    backends._reset_backends()
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(orch, "AI_EMBED_BACKEND", "openai")
    try:
        r = ai_off.post("/ai/embedding", json={"text": "x"})
    finally:
        backends._reset_backends()
    assert r.status_code == 503, r.text
