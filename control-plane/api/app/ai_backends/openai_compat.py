"""OpenAI-compatible engine adapter (LiteLLM, vLLM, OpenAI, Azure OpenAI)."""

from __future__ import annotations

import logging

import httpx

from .base import BaseAIEngine, empty_probe
from .ollama import DEFAULT_MODEL as OLLAMA_DEFAULT_MODEL

logger = logging.getLogger("truenorth.api.ai_backends.openai_compat")

#: LiteLLM alias used when the caller names no model.
DEFAULT_MODEL = "agent"


def _chat_url(base_url: str) -> str:
    """``base_url`` may already end in ``/v1`` (LiteLLM ``.../v1``); never double it."""
    base = base_url.rstrip("/")
    return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"


class OpenAICompatibleEngine(BaseAIEngine):
    def generate(
        self,
        prompt: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        model: str = "",
    ) -> dict:
        # The test endpoint's default model is an Ollama tag no OpenAI-style engine serves.
        if model == OLLAMA_DEFAULT_MODEL:
            model = ""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.post(
            _chat_url(base_url),
            json={
                "model": model or DEFAULT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 256,
            },
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        msg = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"response": msg, "model": data.get("model", "unknown")}

    def probe(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        host: str = "",
        port: int = 0,
        timeout: float = 5.0,
    ) -> dict:
        """``GET {base_url}/models``; the engine serves its models at the base URL."""
        base = base_url.rstrip("/")
        result = empty_probe(base)
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = httpx.get(f"{base}/models", headers=headers, timeout=timeout)
            r.raise_for_status()
            result["models"] = [
                {
                    "name": m.get("id", "unknown"),
                    "size_bytes": 0,
                    "family": m.get("owned_by", ""),
                    "parameter_size": "",
                    "quantization": "",
                }
                for m in r.json().get("data", [])
            ]
            result["running"] = [m["name"] for m in result["models"]]
            result["online"] = True
        except Exception as exc:
            logger.warning("OpenAI-compatible probe failed for %s — %s", base_url, exc)
        return result
