"""Tests for the AI authoring proxies.

No live orchestrator: httpx is monkeypatched so we assert request validation
and transport-fault mapping, not model output.
"""

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _FakeClient:
    """Stand-in for httpx.AsyncClient; behaviour set per test via class attrs."""

    posted = None
    mode = "ok"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        type(self).posted = {"url": url, "json": json}
        if self.mode == "connect_error":
            raise httpx.ConnectError("unreachable")
        if self.mode == "http_error":
            return _FakeResponse(status_code=500, text="model exploded")
        return _FakeResponse(payload={"output": "name: drafted\n", "model_used": "test-model"})


@pytest.fixture
def fake_httpx(monkeypatch):
    _FakeClient.mode = "ok"
    _FakeClient.posted = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


class TestScenarioDraft:
    def test_forwards_to_orchestrator_and_returns_output(self, client, fake_httpx):
        res = client.post(
            "/ai/scenario-draft",
            json={"objectives": ["detect lateral movement"], "difficulty": "advanced", "duration_minutes": 90},
        )
        assert res.status_code == 200
        assert res.json()["output"] == "name: drafted\n"
        assert fake_httpx.posted["url"].endswith("/ai/scenario-suggest")
        assert fake_httpx.posted["json"]["duration_minutes"] == 90

    def test_empty_objectives_rejected_before_any_call(self, client, fake_httpx):
        res = client.post("/ai/scenario-draft", json={"objectives": []})
        assert res.status_code == 422
        assert fake_httpx.posted is None  # never reached the orchestrator

    def test_unreachable_orchestrator_is_503(self, client, fake_httpx):
        fake_httpx.mode = "connect_error"
        res = client.post("/ai/scenario-draft", json={"objectives": ["x"]})
        assert res.status_code == 503

    def test_orchestrator_error_is_502(self, client, fake_httpx):
        fake_httpx.mode = "http_error"
        res = client.post("/ai/scenario-draft", json={"objectives": ["x"]})
        assert res.status_code == 502


class TestDetectionDraft:
    def test_valid_technique_forwards(self, client, fake_httpx):
        res = client.post(
            "/ai/detection-draft",
            json={"technique": "T1059.001", "data_source": "sysmon", "format": "sigma"},
        )
        assert res.status_code == 200
        assert fake_httpx.posted["url"].endswith("/ai/detection-rule")
        assert fake_httpx.posted["json"]["technique"] == "T1059.001"

    def test_bad_technique_pattern_rejected_locally(self, client, fake_httpx):
        res = client.post("/ai/detection-draft", json={"technique": "not-a-technique"})
        assert res.status_code == 422
        assert fake_httpx.posted is None
