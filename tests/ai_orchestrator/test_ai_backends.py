"""Tests for the ai-orchestrator backends package.

Covers:
  - MockAIBackend (deterministic, no network)
  - OpenAIBackend (httpx mocked via respx)
  - AnthropicBackend (httpx mocked via respx)
  - VLLMBackend (httpx mocked via respx, including model discovery)
  - get_cloud_backend factory (registry, singletons, error handling)

Imports use the ``_ai_orch.backends`` namespace registered by conftest.py
to avoid collision with the control-plane ``app`` package.
"""

from __future__ import annotations

import pytest
import respx
from fastapi import HTTPException
from httpx import Response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_backend_singletons():
    """Clear backend singleton cache before and after every test."""
    from _ai_orch.backends import _reset_backends
    _reset_backends()
    yield
    _reset_backends()


# ---------------------------------------------------------------------------
# MockAIBackend
# ---------------------------------------------------------------------------

class TestMockAIBackend:
    @pytest.mark.asyncio
    async def test_generate_returns_mock_string(self):
        from _ai_orch.backends.mock import MockAIBackend
        b = MockAIBackend()
        text, model, usage = await b.generate("Hello")
        assert text.startswith("[MOCK]")
        assert "Hello" in text

    @pytest.mark.asyncio
    async def test_generate_uses_override_model(self):
        from _ai_orch.backends.mock import MockAIBackend
        _, model, _ = await MockAIBackend().generate("p", model="my-model")
        assert model == "my-model"

    @pytest.mark.asyncio
    async def test_generate_default_model_is_mock(self):
        from _ai_orch.backends.mock import MockAIBackend
        _, model, _ = await MockAIBackend().generate("p")
        assert model == "mock"

    @pytest.mark.asyncio
    async def test_generate_truncates_long_prompt(self):
        from _ai_orch.backends.mock import MockAIBackend
        long_prompt = "x" * 1000
        text, _, _ = await MockAIBackend().generate(long_prompt)
        assert "..." in text
        assert len(text) < 300  # should be truncated in the preview

    @pytest.mark.asyncio
    async def test_health_check_always_true(self):
        from _ai_orch.backends.mock import MockAIBackend
        assert await MockAIBackend().health_check() is True

    @pytest.mark.asyncio
    async def test_usage_contains_token_counts(self):
        from _ai_orch.backends.mock import MockAIBackend
        _, _, usage = await MockAIBackend().generate("token count test")
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "total_tokens" in usage


# ---------------------------------------------------------------------------
# OpenAIBackend
# ---------------------------------------------------------------------------

class TestOpenAIBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_success(self):
        from _ai_orch.backends.openai import OpenAIBackend

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [{"message": {"content": "Hello from GPT"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                },
            )
        )
        b = OpenAIBackend(api_key="sk-test")
        text, model, usage = await b.generate("Hi", model="gpt-4o", max_tokens=100)
        assert text == "Hello from GPT"
        assert model == "gpt-4o"
        assert usage["total_tokens"] == 9

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_uses_default_model(self):
        from _ai_orch.backends.openai import OpenAIBackend

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})
        )
        b = OpenAIBackend(api_key="sk-test", default_model="gpt-4o-mini")
        _, model, _ = await b.generate("prompt")
        assert model == "gpt-4o-mini"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_system_prompt_included(self):
        from _ai_orch.backends.openai import OpenAIBackend

        captured = {}

        async def handler(req, *args, **kwargs):
            import json
            captured["body"] = json.loads(req.content)
            return Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

        respx.post("https://api.openai.com/v1/chat/completions").mock(side_effect=handler)
        await OpenAIBackend(api_key="sk-test").generate("prompt", system_prompt="You are helpful.")
        messages = captured["body"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_raises_502_on_http_error(self):
        from _ai_orch.backends.openai import OpenAIBackend

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(401, text="Unauthorized")
        )
        with pytest.raises(HTTPException) as exc_info:
            await OpenAIBackend(api_key="bad-key").generate("Hi")
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_raises_503_on_connection_error(self):
        import httpx
        from _ai_orch.backends.openai import OpenAIBackend

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(HTTPException) as exc_info:
            await OpenAIBackend(api_key="sk-test").generate("Hi")
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    @respx.mock
    async def test_health_check_true_on_200(self):
        from _ai_orch.backends.openai import OpenAIBackend

        respx.get("https://api.openai.com/v1/models").mock(return_value=Response(200, json={"data": []}))
        assert await OpenAIBackend(api_key="sk-test").health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_without_key(self):
        from _ai_orch.backends.openai import OpenAIBackend

        assert await OpenAIBackend(api_key="").health_check() is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_custom_base_url(self):
        from _ai_orch.backends.openai import OpenAIBackend

        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "groq-ok"}}], "usage": {}})
        )
        b = OpenAIBackend(api_key="groq-key", base_url="https://api.groq.com/openai/v1")
        text, _, _ = await b.generate("Hi", model="llama3-8b-8192")
        assert text == "groq-ok"


# ---------------------------------------------------------------------------
# AnthropicBackend
# ---------------------------------------------------------------------------

class TestAnthropicBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_success(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "content": [{"type": "text", "text": "Claude says hi"}],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            )
        )
        b = AnthropicBackend(api_key="sk-ant-test")
        text, model, usage = await b.generate("Hello", model="claude-3-haiku-20240307", max_tokens=50)
        assert text == "Claude says hi"
        assert model == "claude-3-haiku-20240307"
        assert usage["input_tokens"] == 3

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_uses_default_model(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})
        )
        _, model, _ = await AnthropicBackend(api_key="x", default_model="claude-3-sonnet").generate("p")
        assert model == "claude-3-sonnet"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_system_prompt_in_body(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        captured = {}

        async def handler(req, *args, **kwargs):
            import json
            captured["body"] = json.loads(req.content)
            return Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

        respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=handler)
        await AnthropicBackend(api_key="x").generate("prompt", system_prompt="Be concise.")
        assert captured["body"]["system"] == "Be concise."

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_empty_content_returns_empty_string(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(200, json={"content": [], "usage": {}})
        )
        text, _, _ = await AnthropicBackend(api_key="x").generate("p")
        assert text == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_raises_502_on_http_error(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        respx.post("https://api.anthropic.com/v1/messages").mock(return_value=Response(429, text="Rate limit"))
        with pytest.raises(HTTPException) as exc_info:
            await AnthropicBackend(api_key="x").generate("p")
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_health_check_true_with_key(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        assert await AnthropicBackend(api_key="sk-ant-x").health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_without_key(self):
        from _ai_orch.backends.anthropic import AnthropicBackend

        assert await AnthropicBackend(api_key="").health_check() is False


# ---------------------------------------------------------------------------
# VLLMBackend
# ---------------------------------------------------------------------------

class TestVLLMBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_success(self):
        from _ai_orch.backends.vllm import VLLMBackend

        respx.post("http://vllm-test:8000/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [{"message": {"content": "vLLM response"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                },
            )
        )
        b = VLLMBackend(url="http://vllm-test:8000", default_model="llama3")
        text, model, usage = await b.generate("Hello", model="llama3", max_tokens=100)
        assert text == "vLLM response"
        assert model == "llama3"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_discovers_model_when_empty(self):
        from _ai_orch.backends.vllm import VLLMBackend

        respx.get("http://vllm-test:8000/v1/models").mock(
            return_value=Response(200, json={"data": [{"id": "meta-llama/Llama-3.3-70B-Instruct"}]})
        )
        respx.post("http://vllm-test:8000/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})
        )
        b = VLLMBackend(url="http://vllm-test:8000")  # no default_model
        _, model, _ = await b.generate("Hi")
        assert model == "meta-llama/Llama-3.3-70B-Instruct"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_bearer_auth_header_sent(self):
        from _ai_orch.backends.vllm import VLLMBackend

        captured = {}

        async def handler(req, *args, **kwargs):
            captured["auth"] = req.headers.get("Authorization")
            return Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

        respx.post("http://vllm-test:8000/v1/chat/completions").mock(side_effect=handler)
        await VLLMBackend(url="http://vllm-test:8000", api_key="secret", default_model="m").generate("p")
        assert captured["auth"] == "Bearer secret"

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_no_auth_header_without_key(self):
        from _ai_orch.backends.vllm import VLLMBackend

        captured = {}

        async def handler(req, *args, **kwargs):
            captured["auth"] = req.headers.get("Authorization")
            return Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

        respx.post("http://vllm-test:8000/v1/chat/completions").mock(side_effect=handler)
        await VLLMBackend(url="http://vllm-test:8000", api_key="", default_model="m").generate("p")
        assert "auth" not in captured or captured["auth"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_generate_raises_502_on_http_error(self):
        from _ai_orch.backends.vllm import VLLMBackend

        respx.post("http://vllm-test:8000/v1/chat/completions").mock(return_value=Response(500, text="Error"))
        with pytest.raises(HTTPException) as exc_info:
            await VLLMBackend(url="http://vllm-test:8000", default_model="m").generate("p")
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    @respx.mock
    async def test_health_check_true_on_200(self):
        from _ai_orch.backends.vllm import VLLMBackend

        respx.get("http://vllm-test:8000/health").mock(return_value=Response(200))
        assert await VLLMBackend(url="http://vllm-test:8000").health_check() is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_health_check_false_on_connection_error(self):
        import httpx
        from _ai_orch.backends.vllm import VLLMBackend

        respx.get("http://vllm-test:8000/health").mock(side_effect=httpx.ConnectError("refused"))
        assert await VLLMBackend(url="http://vllm-test:8000").health_check() is False


# ---------------------------------------------------------------------------
# Factory — get_cloud_backend
# ---------------------------------------------------------------------------

class TestGetCloudBackend:
    def test_returns_mock_backend(self):
        from _ai_orch.backends import get_cloud_backend
        from _ai_orch.backends.mock import MockAIBackend

        b = get_cloud_backend("mock")
        assert isinstance(b, MockAIBackend)

    def test_returns_openai_backend(self):
        from _ai_orch.backends import get_cloud_backend
        from _ai_orch.backends.openai import OpenAIBackend

        b = get_cloud_backend("openai")
        assert isinstance(b, OpenAIBackend)

    def test_returns_anthropic_backend(self):
        from _ai_orch.backends import get_cloud_backend
        from _ai_orch.backends.anthropic import AnthropicBackend

        b = get_cloud_backend("anthropic")
        assert isinstance(b, AnthropicBackend)

    def test_returns_vllm_backend(self):
        from _ai_orch.backends import get_cloud_backend
        from _ai_orch.backends.vllm import VLLMBackend

        b = get_cloud_backend("vllm")
        assert isinstance(b, VLLMBackend)

    def test_returns_singleton(self):
        from _ai_orch.backends import get_cloud_backend

        a = get_cloud_backend("mock")
        b = get_cloud_backend("mock")
        assert a is b

    def test_raises_on_unknown_backend(self):
        from _ai_orch.backends import get_cloud_backend

        with pytest.raises(ValueError, match="Unknown AI backend"):
            get_cloud_backend("nonexistent")

    def test_case_insensitive(self):
        from _ai_orch.backends import get_cloud_backend
        from _ai_orch.backends.mock import MockAIBackend

        assert isinstance(get_cloud_backend("MOCK"), MockAIBackend)

    def test_reset_clears_singletons(self):
        from _ai_orch.backends import _reset_backends, get_cloud_backend

        a = get_cloud_backend("mock")
        _reset_backends()
        b = get_cloud_backend("mock")
        assert a is not b
