"""With no AI endpoint configured the orchestrator says so instead of calling out.

OLLAMA_PROXY_URL used to default to one person's internet-facing host. It was never
read, but it advertised a default endpoint; it is gone. The orchestrator has no default
AI endpoint: Ollama nodes come only from OLLAMA_NODES, cloud backends only from keys.
"""

from __future__ import annotations

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
