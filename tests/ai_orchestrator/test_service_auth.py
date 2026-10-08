"""The ai-orchestrator is not an open service.

Before: all fifteen routes answered anyone who could reach port 6000 (nginx even published
it as /ai/), CORS allowed every origin, and POST /fleet/nodes registered any URL as an
Ollama node, after which the orchestrator sent requests to it (cloud metadata included).
Now every route but /health needs AI_SERVICE_TOKEN, CORS is off unless origins are
listed, and runtime node URLs are validated. The route walk below is the auth-coverage
guard for this service: a new route without the token check fails it by construction.
"""

from __future__ import annotations

import importlib
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

orch = importlib.import_module("_ai_orch.main")

TOKEN = "orchestrator-service-token-0123456789abcdef"
PUBLIC = {"/health"}


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", TOKEN)
    return TestClient(orch.app)  # no lifespan: no health loop, no node clients


def _routes() -> list[tuple[str, str]]:
    out = []
    for route in orch.app.routes:
        if isinstance(route, APIRoute):
            path = re.sub(r"\{[^}]+\}", "gpu-test", route.path)
            out.extend((m, path) for m in sorted(route.methods - {"HEAD", "OPTIONS"}))
    return out


def test_the_walk_sees_the_whole_api():
    paths = {p for _, p in _routes()}
    assert len(_routes()) >= 15, _routes()
    assert {"/fleet/nodes", "/fleet/gpu-test/pull", "/ai/generate", "/ai/exercise-forge"} <= paths


@pytest.mark.parametrize(("method", "path"), [r for r in _routes() if r[1] not in PUBLIC])
def test_every_route_but_health_refuses_a_caller_without_the_token(secured, method, path):
    assert secured.request(method, path, json={}).status_code == 401
    wrong = {"Authorization": "Bearer not-the-token"}
    assert secured.request(method, path, json={}, headers=wrong).status_code == 401
    assert secured.request(method, path, json={}, headers={"X-Service-Token": TOKEN[:-1]}).status_code == 401


def test_health_stays_open_for_probes_and_does_not_leak_node_urls(secured, monkeypatch):
    monkeypatch.setitem(orch.FLEET, "gpu-x", orch.OllamaNode(name="gpu-x", base_url="http://10.9.8.7:11434"))
    r = secured.get("/health")
    assert r.status_code == 200
    assert "10.9.8.7" not in r.text


@pytest.mark.parametrize("headers", [{"Authorization": f"Bearer {TOKEN}"}, {"X-Service-Token": TOKEN}])
def test_the_token_opens_the_api(secured, headers):
    assert secured.get("/fleet", headers=headers).status_code == 200


def test_production_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setattr(orch, "TN_ENV", "production")
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "")
    assert orch.startup_problems()
    with pytest.raises(RuntimeError, match="AI_SERVICE_TOKEN"), TestClient(orch.app):
        pass
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "short")
    assert "shorter" in orch.startup_problems()[0]
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", TOKEN)
    assert orch.startup_problems() == []


def test_production_never_fails_open(monkeypatch):
    monkeypatch.setattr(orch, "TN_ENV", "production")
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "")
    assert TestClient(orch.app).get("/fleet").status_code == 503


def test_development_without_a_token_stays_open(monkeypatch):
    monkeypatch.setattr(orch, "TN_ENV", "development")
    monkeypatch.setattr(orch, "AI_SERVICE_TOKEN", "")
    assert TestClient(orch.app).get("/fleet").status_code == 200


def test_cors_is_off_by_default_and_never_a_wildcard():
    assert "*" not in orch.CORS_ORIGINS
    r = TestClient(orch.app).options(
        "/ai/generate", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# -- runtime fleet nodes --------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fd00:ec2::254]/",
        "http://169.254.1.1:11434",
        "http://127.0.0.1:6000",
        "http://localhost:11434",
        "http://0.0.0.0:11434",
        "file:///etc/passwd",
        "gopher://10.0.0.5:70/",
        "http://user:pw@10.0.0.5:11434",
        "http://:11434",
        "http://10.0.0.5:notaport",
    ],
)
def test_unsafe_node_urls_are_refused(url):
    with pytest.raises(ValueError):
        orch.validate_node_url(url)


def test_a_lan_node_is_accepted_and_normalised():
    assert orch.validate_node_url("http://10.0.0.12:11434/") == "http://10.0.0.12:11434"


def test_the_allowlist_narrows_what_is_accepted(monkeypatch):
    monkeypatch.setattr(orch, "OLLAMA_NODE_ALLOWLIST", ["10.0.0.0/24", "gpu.lab.test"])
    assert orch.validate_node_url("http://10.0.0.12:11434")
    with pytest.raises(ValueError, match="ALLOWLIST"):
        orch.validate_node_url("http://10.0.1.12:11434")


def test_registering_a_metadata_node_is_a_422_and_registers_nothing(secured):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = secured.post("/fleet/nodes", json={"name": "evil", "url": "http://169.254.169.254"}, headers=auth)
    assert r.status_code == 422
    assert "evil" not in orch.FLEET


def test_registering_a_lan_node_works(secured):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        r = secured.post("/fleet/nodes", json={"name": "gpu-lan", "url": "http://10.0.0.12:11434/"}, headers=auth)
        assert r.status_code == 200, r.text
        assert orch.FLEET["gpu-lan"].base_url == "http://10.0.0.12:11434"
    finally:
        orch.FLEET.pop("gpu-lan", None)
        orch._ollama_backends.pop("gpu-lan", None)
        orch._node_semaphores.pop("gpu-lan", None)


def test_a_pull_with_a_malformed_model_name_is_refused(secured, monkeypatch):
    monkeypatch.setitem(orch.FLEET, "gpu-x", orch.OllamaNode(name="gpu-x", base_url="http://10.0.0.12:11434"))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = secured.post("/fleet/gpu-x/pull", params={"model": "../../etc?x=1"}, headers=auth)
    assert r.status_code == 422
