"""/ai-config talks to inference engines only through app.ai_backends (ADR 0001).

Adapter unit tests (registry, URL/payload shape, probe failure) and the two
router paths that use them — test-generate and discover — including the error
paths: engine down, and a backend_type no adapter knows.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from app.ai_backends import (
    MockEngine,
    OllamaEngine,
    OpenAICompatibleEngine,
    get_ai_engine,
    known_engine_types,
)
from app.models import AIBackendConfig, AIFleetNode
from httpx import Response

LITELLM = "http://litellm:4000/v1"


# ── Registry ──────────────────────────────────────────────────────────────


class TestRegistry:
    @pytest.mark.parametrize("kind", ["openai", "azure_openai", "vllm", "litellm", "anthropic"])
    def test_openai_compatible_types(self, kind):
        assert isinstance(get_ai_engine(kind), OpenAICompatibleEngine)

    def test_ollama_and_mock(self):
        assert isinstance(get_ai_engine("ollama"), OllamaEngine)
        assert get_ai_engine("ollama").uses_fleet_nodes is True
        assert isinstance(get_ai_engine("MOCK"), MockEngine)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown AI backend type"):
            get_ai_engine("carrier-pigeon")

    def test_unknown_type_with_default(self):
        assert isinstance(get_ai_engine("carrier-pigeon", default="mock"), MockEngine)

    def test_known_types_listed(self):
        assert {"ollama", "openai", "mock"} <= set(known_engine_types())


# ── Adapters ──────────────────────────────────────────────────────────────


class TestOpenAICompatibleEngine:
    @respx.mock
    def test_generate_does_not_double_v1_and_sends_bearer(self):
        route = respx.post(f"{LITELLM}/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "pong"}}], "model": "agent"})
        )
        out = OpenAICompatibleEngine().generate("ping", base_url=LITELLM, api_key="sk-1", timeout=5)
        assert out == {"response": "pong", "model": "agent"}
        req = route.calls[0].request
        assert req.headers["Authorization"] == "Bearer sk-1"
        assert json.loads(req.content)["model"] == "agent"

    @respx.mock
    def test_generate_appends_v1_and_drops_ollama_default_model(self):
        route = respx.post("http://vllm:8000/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "x"}}]})
        )
        OpenAICompatibleEngine().generate("p", base_url="http://vllm:8000", model="llama3.1:latest")
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "agent" and body["max_tokens"] == 256
        assert "Authorization" not in route.calls[0].request.headers

    @respx.mock
    def test_generate_raises_when_engine_refuses(self):
        respx.post(f"{LITELLM}/chat/completions").mock(return_value=Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            OpenAICompatibleEngine().generate("p", base_url=LITELLM)

    @respx.mock
    def test_probe_lists_models(self):
        respx.get(f"{LITELLM}/models").mock(
            return_value=Response(200, json={"data": [{"id": "agent", "owned_by": "litellm"}]})
        )
        probe = OpenAICompatibleEngine().probe(base_url=LITELLM)
        assert probe["online"] is True
        assert [m["name"] for m in probe["models"]] == ["agent"] == probe["running"]

    @respx.mock
    def test_probe_down_is_offline_not_an_exception(self):
        respx.get(f"{LITELLM}/models").mock(side_effect=httpx.ConnectError("down"))
        probe = OpenAICompatibleEngine().probe(base_url=LITELLM)
        assert probe["online"] is False and probe["models"] == []


class TestOllamaEngine:
    @respx.mock
    def test_generate(self):
        route = respx.post("http://gpu1:11434/api/generate").mock(
            return_value=Response(200, json={"response": "hi", "model": "llama3.1:latest"})
        )
        out = OllamaEngine().generate("p", base_url="http://gpu1:11434/")
        assert out == {"response": "hi", "model": "llama3.1:latest"}
        assert json.loads(route.calls[0].request.content) == {
            "model": "llama3.1:latest",
            "prompt": "p",
            "stream": False,
        }

    @respx.mock
    def test_probe_node(self):
        respx.get("http://10.0.0.5:11434/api/version").mock(return_value=Response(200, json={"version": "0.5"}))
        respx.get("http://10.0.0.5:11434/api/tags").mock(
            return_value=Response(200, json={"models": [{"name": "qwen:7b", "size": 7}]})
        )
        respx.get("http://10.0.0.5:11434/api/ps").mock(
            return_value=Response(200, json={"models": [{"name": "qwen:7b"}]})
        )
        probe = OllamaEngine().probe(base_url="", host="10.0.0.5", port=11434)
        assert probe["online"] and probe["version"] == "0.5" and probe["running"] == ["qwen:7b"]

    @respx.mock
    def test_probe_down_is_offline(self):
        respx.get("http://10.0.0.5:11434/api/version").mock(side_effect=httpx.ConnectError("down"))
        assert OllamaEngine().probe(base_url="", host="10.0.0.5", port=11434)["online"] is False


def test_mock_engine_needs_no_network():
    assert MockEngine().generate("hello")["response"] == "[Mock AI] Echo: hello"
    assert MockEngine().probe()["online"] is True


# ── Router: /ai-config/test-generate and /discover ────────────────────────


def _primary(db, backend_type: str, base_url: str = LITELLM) -> AIBackendConfig:
    b = AIBackendConfig(
        name=f"{backend_type}-primary",
        backend_type=backend_type,
        base_url=base_url,
        api_key_encrypted="sk-1",
        is_primary=True,
        is_active=True,
        timeout_seconds=5,
    )
    db.add(b)
    db.commit()
    return b


class TestTestGenerate:
    def test_no_primary_answers_from_mock(self, client):
        r = client.post("/ai-config/test-generate", params={"prompt": "hi"})
        assert r.status_code == 200
        assert r.json()["backend"] == "mock (no primary configured)"
        assert r.json()["response"] == "[Mock AI] Echo: hi"

    @respx.mock
    def test_openai_compatible_primary(self, client, db_session):
        _primary(db_session, "litellm")
        respx.post(f"{LITELLM}/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "pong"}}], "model": "agent"})
        )
        r = client.post("/ai-config/test-generate", params={"prompt": "ping"})
        body = r.json()
        assert r.status_code == 200
        assert (body["response"], body["backend_type"], body["model"]) == ("pong", "litellm", "agent")
        assert "node" not in body

    @respx.mock
    def test_engine_down_reports_unreachable(self, client, db_session):
        _primary(db_session, "openai")
        respx.post(f"{LITELLM}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
        r = client.post("/ai-config/test-generate", params={"prompt": "ping"})
        assert r.status_code == 200
        assert r.json()["model"] == "error"
        assert r.json()["response"].startswith("[Backend unreachable]")

    def test_unknown_backend_type_answers_from_mock(self, client, db_session):
        _primary(db_session, "carrier-pigeon")
        r = client.post("/ai-config/test-generate", params={"prompt": "coo"})
        assert r.status_code == 200
        assert r.json()["response"] == "[Mock AI] Echo: coo"
        assert r.json()["backend_type"] == "carrier-pigeon"

    @respx.mock
    def test_ollama_primary_routes_through_fleet_node(self, client, db_session):
        b = _primary(db_session, "ollama", base_url="http://open-webui:3000")
        db_session.add(
            AIFleetNode(
                backend_id=b.id,
                node_name="gpu1",
                url="http://gpu1:11434",
                status="online",
                loaded_models=json.dumps(["llama3.1:latest"]),
            )
        )
        db_session.commit()
        route = respx.post("http://gpu1:11434/api/generate").mock(
            return_value=Response(200, json={"response": "from-node", "model": "llama3.1:latest"})
        )
        r = client.post("/ai-config/test-generate", params={"prompt": "p"})
        assert r.json()["response"] == "from-node"
        assert r.json()["node"] == "gpu1 (http://gpu1:11434)"
        assert route.called


class TestDiscover:
    @respx.mock
    def test_openai_compatible_backend_probes_base_url(self, client, db_session):
        b = _primary(db_session, "vllm")
        respx.get(f"{LITELLM}/models").mock(return_value=Response(200, json={"data": [{"id": "agent"}]}))
        r = client.post(f"/ai-config/backends/{b.id}/discover")
        assert r.status_code == 200
        result = r.json()["results"][0]
        assert result["online"] is True and result["url"] == LITELLM
        node = db_session.query(AIFleetNode).filter(AIFleetNode.backend_id == b.id).one()
        assert node.status == "online" and json.loads(node.loaded_models) == ["agent"]

    def test_unknown_backend_404(self, client):
        r = client.post("/ai-config/backends/00000000-0000-0000-0000-00000000dead/discover")
        assert r.status_code == 404
