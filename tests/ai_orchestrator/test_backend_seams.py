"""The orchestrator reaches every model server through a BaseAIBackend.

Covers the Ollama node backend, the embeddings capability on the ABC (OpenAI,
Ollama, Mock, and the 501 default), the mock's parseable authoring fixtures,
the system-prompt fix on the Anthropic/fallback paths, and the error paths
(backend down, unknown provider) as the HTTP API reports them.

``main.py`` is imported as ``_ai_orch.main`` so its lazy ``from .backends``
imports resolve to the package the conftest registered.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest
import respx
import yaml
from fastapi import HTTPException
from httpx import Response

ollama_mod = importlib.import_module("_ai_orch.backends.ollama")
mock_mod = importlib.import_module("_ai_orch.backends.mock")
backends = importlib.import_module("_ai_orch.backends")
main = importlib.import_module("_ai_orch.main")

OllamaBackend = ollama_mod.OllamaBackend
NODE_URL = "http://gpu1:11434"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    backends._reset_backends()
    monkeypatch.setattr(main, "_llm_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(main, "_cache", {})

    async def _no_sleep(_delay):
        return None

    # _call_with_retry backs off for seconds; the retries are what we test, not the wait.
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    yield
    backends._reset_backends()


def _node_backend() -> OllamaBackend:
    return OllamaBackend(base_url=NODE_URL, client=httpx.AsyncClient(base_url=NODE_URL))


def _fleet(monkeypatch, models=("llama3.1:70b-instruct",)):
    node = main.OllamaNode(name="gpu1", base_url=NODE_URL)
    node.models = list(models)
    node.tagged_models = {m: main._tag_model(m) for m in models}
    monkeypatch.setattr(main, "FLEET", {"gpu1": node})
    monkeypatch.setattr(main, "_ollama_backends", {"gpu1": _node_backend()})
    monkeypatch.setattr(main, "_node_semaphores", {"gpu1": asyncio.Semaphore(2)})
    return node


def _asgi():
    # No lifespan: the health loop and real node clients stay out of the test.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://orch")


# ── OllamaBackend ─────────────────────────────────────────────────────────


class TestOllamaBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_posts_chat_completions_with_system_prompt(self):
        route = respx.post(f"{NODE_URL}/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 3}})
        )
        text, model, usage = await _node_backend().generate("p", model="m1", system_prompt="be terse")
        assert (text, model, usage["total_tokens"]) == ("hi", "m1", 3)
        body = json.loads(route.calls[0].request.content)
        assert body["messages"][0] == {"role": "system", "content": "be terse"}
        assert body["stream"] is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_leaves_transport_errors_unwrapped_for_retry(self):
        respx.post(f"{NODE_URL}/v1/chat/completions").mock(return_value=Response(503, text="busy"))
        with pytest.raises(httpx.HTTPStatusError):
            await _node_backend().generate("p", model="m1")

    @pytest.mark.asyncio
    @respx.mock
    async def test_embed_and_list_models(self):
        respx.post(f"{NODE_URL}/api/embeddings").mock(return_value=Response(200, json={"embedding": [0.1, 0.2]}))
        respx.get(f"{NODE_URL}/api/tags").mock(return_value=Response(200, json={"models": [{"name": "bge-m3:latest"}]}))
        b = _node_backend()
        assert await b.embed("text") == ([0.1, 0.2], "bge-m3:latest")
        assert await b.list_models() == ["bge-m3:latest"]
        assert await b.health_check() is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_health_check_false_when_node_down(self):
        respx.get(f"{NODE_URL}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        assert await _node_backend().health_check() is False

    def test_registry_knows_ollama(self):
        assert isinstance(backends.get_cloud_backend("ollama"), OllamaBackend)

    def test_registry_rejects_unknown_provider(self):
        with pytest.raises(ValueError, match="Valid options"):
            backends.get_cloud_backend("not-a-provider")


# ── Embeddings on the ABC ─────────────────────────────────────────────────


class TestEmbeddings:
    @pytest.mark.asyncio
    @respx.mock
    async def test_openai_embed(self):
        route = respx.post("http://litellm:4000/v1/embeddings").mock(
            return_value=Response(200, json={"data": [{"embedding": [1.0, 2.0]}], "model": "embed"})
        )
        b = backends.OpenAIBackend(api_key="k", base_url="http://litellm:4000/v1")
        assert await b.embed("x", model="embed") == ([1.0, 2.0], "embed")
        assert json.loads(route.calls[0].request.content) == {"model": "embed", "input": "x"}

    @pytest.mark.asyncio
    async def test_mock_embed_is_deterministic(self, monkeypatch):
        monkeypatch.setenv("MOCK_EMBED_DIM", "16")
        m = backends.MockAIBackend()
        v1, model = await m.embed("same")
        v2, _ = await m.embed("same")
        v3, _ = await m.embed("other")
        assert v1 == v2 and v1 != v3
        assert len(v1) == 16 and model == "mock-embed"
        assert all(-1.0 <= x <= 1.0 for x in v1)

    @pytest.mark.asyncio
    async def test_backend_without_embeddings_refuses_501(self):
        with pytest.raises(HTTPException) as exc:
            await backends.AnthropicBackend(api_key="k").embed("x")
        assert exc.value.status_code == 501


# ── Mock fixtures the control plane can parse ─────────────────────────────


class TestMockFixtures:
    @pytest.mark.asyncio
    async def test_forge_prompt_gets_persistable_yaml(self):
        text, _, _ = await backends.MockAIBackend().generate(
            "Generate a complete cyber training exercise scenario as YAML for the TrueNorth Range platform."
        )
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict) and parsed["name"]
        assert sum(o["points"] for o in parsed["objectives"]) == 100
        assert "[MOCK]" in text

    @pytest.mark.asyncio
    async def test_course_prompt_gets_course_json(self):
        text, _, _ = await backends.MockAIBackend().generate("Draft a complete course grounded ONLY in ...")
        draft = json.loads(text)
        assert draft["modules"] and draft["name"].startswith("[MOCK]")

    @pytest.mark.asyncio
    async def test_other_prompts_still_echo(self):
        text, _, _ = await backends.MockAIBackend().generate("hello")
        assert text.startswith("[MOCK] Response for: hello")


# ── system_prompt reaches every provider ──────────────────────────────────


def _anthropic_route():
    return respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=Response(200, json={"content": [{"text": "ok"}], "usage": {}})
    )


class TestSystemPrompt:
    @pytest.mark.asyncio
    @respx.mock
    async def test_call_anthropic_passes_system_prompt(self):
        backends._instances["anthropic"] = backends.AnthropicBackend(api_key="k")
        route = _anthropic_route()
        text, _, backend, _ = await main._call_anthropic("p", "claude-x", 100, system_prompt="sys")
        assert (text, backend) == ("ok", "anthropic")
        assert json.loads(route.calls[0].request.content)["system"] == "sys"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_with_anthropic_primary_passes_system_prompt(self, monkeypatch):
        monkeypatch.setattr(main, "PRIMARY_BACKEND", main.BackendType.anthropic)
        monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "k")
        backends._instances["anthropic"] = backends.AnthropicBackend(api_key="k")
        route = _anthropic_route()
        await main._generate("p", system_prompt="sys", use_cache=False)
        assert json.loads(route.calls[0].request.content)["system"] == "sys"

    @pytest.mark.asyncio
    @respx.mock
    async def test_cloud_fallback_passes_system_prompt(self, monkeypatch):
        monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "k")
        backends._instances["anthropic"] = backends.AnthropicBackend(api_key="k")
        route = _anthropic_route()
        r = main.ModelRoute(task=main.TaskType.general, tags=[], fallback_backend=main.BackendType.anthropic)
        await main._cloud_fallback("p", r, 100, system_prompt="sys")
        assert json.loads(route.calls[0].request.content)["system"] == "sys"

    @pytest.mark.asyncio
    async def test_system_prompt_is_part_of_the_cache_key(self, monkeypatch):
        monkeypatch.setattr(main, "PRIMARY_BACKEND", main.BackendType.mock)
        a = await main._generate("same", system_prompt="one")
        b = await main._generate("same", system_prompt="two")
        assert a[4] is False and b[4] is False  # second call is not served from the first's cache


# ── Fleet path goes through OllamaBackend ─────────────────────────────────


class TestFleetThroughBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_call_ollama_routes_via_node_backend(self, monkeypatch):
        node = _fleet(monkeypatch)
        respx.post(f"{NODE_URL}/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "fleet"}}], "usage": {}})
        )
        text, model, node_name, usage = await main._call_ollama("p", "llama3.1:70b-instruct")
        assert (text, model, node_name, usage["node"]) == ("fleet", "llama3.1:70b-instruct", "gpu1", "gpu1")
        assert node._inflight == 0 and node.healthy

    @pytest.mark.asyncio
    @respx.mock
    async def test_node_down_falls_back_to_mock(self, monkeypatch):
        _fleet(monkeypatch)
        monkeypatch.setattr(main, "PRIMARY_BACKEND", main.BackendType.ollama)
        monkeypatch.setattr(main, "OPENAI_API_KEY", "")
        route = respx.post(f"{NODE_URL}/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
        text, _, backend, _, _ = await main._generate("p", main.TaskType.scenario_suggest, use_cache=False)
        assert route.call_count == 3  # first try + 2 retries
        assert backend == "mock" and text.startswith("[MOCK]")

    @pytest.mark.asyncio
    async def test_no_healthy_node_is_503(self, monkeypatch):
        node = _fleet(monkeypatch)
        node.healthy = False
        with pytest.raises(HTTPException) as exc:
            await main._call_ollama("p", "llama3.1:70b-instruct")
        assert exc.value.status_code == 503


# ── HTTP API: happy path and error paths ──────────────────────────────────


class TestHttpApi:
    @pytest.mark.asyncio
    async def test_embedding_via_mock_backend(self, monkeypatch):
        monkeypatch.setattr(main, "AI_EMBED_BACKEND", "mock")
        async with _asgi() as c:
            r = await c.post("/ai/embedding", json={"text": "hello"})
        assert r.status_code == 200
        assert r.json()["node_used"] == "mock" and len(r.json()["embedding"]) > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_embedding_backend_down_is_503(self, monkeypatch):
        monkeypatch.setattr(main, "AI_EMBED_BACKEND", "openai")
        backends._instances["openai"] = backends.OpenAIBackend(api_key="k", base_url="http://litellm:4000/v1")
        route = respx.post("http://litellm:4000/v1/embeddings").mock(side_effect=httpx.ConnectError("down"))
        async with _asgi() as c:
            r = await c.post("/ai/embedding", json={"text": "hello"})
        assert r.status_code == 503
        assert route.call_count == 3  # retried before giving up

    @pytest.mark.asyncio
    async def test_embedding_unknown_provider_is_503(self, monkeypatch):
        monkeypatch.setattr(main, "AI_EMBED_BACKEND", "no-such-provider")
        async with _asgi() as c:
            r = await c.post("/ai/embedding", json={"text": "hello"})
        assert r.status_code == 503
        assert "Unknown AI backend" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_generate_accepts_system_prompt(self, monkeypatch):
        monkeypatch.setattr(main, "PRIMARY_BACKEND", main.BackendType.mock)
        async with _asgi() as c:
            r = await c.post(
                "/ai/generate",
                json={"task": "general", "prompt": "hi", "system_prompt": "be terse", "skip_cache": True},
            )
        assert r.status_code == 200
        assert r.json()["usage"]["system_prompt_chars"] == len("be terse")

    @pytest.mark.asyncio
    async def test_exercise_forge_with_mock_returns_scenario_yaml(self, monkeypatch):
        monkeypatch.setattr(main, "PRIMARY_BACKEND", main.BackendType.mock)
        async with _asgi() as c:
            r = await c.post(
                "/ai/exercise-forge",
                json={"threat_indicators": [{"indicator_type": "domain", "value": "evil.example"}]},
            )
        assert r.status_code == 200
        assert yaml.safe_load(r.json()["output"])["objectives"]
