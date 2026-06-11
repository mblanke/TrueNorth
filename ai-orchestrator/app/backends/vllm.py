"""TrueNorth Range AI Orchestrator — vLLM backend.

vLLM exposes an OpenAI-compatible API, making it a drop-in for
self-hosted LLMs on the H200 cluster (or any GPU node running vLLM).

Configuration (env vars):
    VLLM_URL            — vLLM server URL           (default: http://vllm:8000)
    VLLM_API_KEY        — Bearer token if auth enabled (default: "")
    VLLM_DEFAULT_MODEL  — Default model name        (default: "")
                          Set to the loaded model's huggingface ID or alias,
                          e.g. "meta-llama/Llama-3.3-70B-Instruct"

Use case:
    On the H200 nodes, vLLM serves one large model per node with higher
    throughput than Ollama.  Set AI_MODEL_BACKEND=vllm to route
    generation tasks here instead of the Ollama fleet.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.vllm")


class VLLMBackend(BaseAIBackend):
    """vLLM OpenAI-compatible inference backend.

    Works with any vLLM-served model.  Uses the /v1/chat/completions
    endpoint (stream=False).
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._url = (url or os.getenv("VLLM_URL", "http://vllm:8000")).rstrip("/")
        self._api_key = api_key if api_key is not None else os.getenv("VLLM_API_KEY", "")
        self._default_model = default_model or os.getenv("VLLM_DEFAULT_MODEL", "")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        effective_model = model or self._default_model
        if not effective_model:
            # vLLM with a single loaded model will accept an empty model field
            # or the server's model alias; query /v1/models to discover it
            effective_model = await self._discover_model()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self._url}/v1/chat/completions",
                    json={
                        "model": effective_model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                        "stream": False,
                    },
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return text, effective_model, usage
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"vLLM error {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except Exception as exc:
            raise HTTPException(503, f"vLLM backend unavailable: {exc}") from exc

    async def _discover_model(self) -> str:
        """Query the vLLM /v1/models endpoint to discover the first loaded model."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/v1/models", headers=self._headers())
                resp.raise_for_status()
                models = resp.json().get("data", [])
                if models:
                    return models[0]["id"]
        except Exception as exc:
            logger.warning("vLLM model discovery failed: %s", exc)
        return ""

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/health", headers=self._headers())
                return resp.status_code < 500
        except Exception:
            return False
