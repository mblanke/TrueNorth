"""TrueNorth Range AI Orchestrator — Ollama node backend.

One instance speaks to ONE Ollama server. The fleet manager in ``main.py``
(node selection, per-node semaphores, in-flight counts, health tags and retry)
owns a dict of these, one per node, and is the only thing that chooses between
them. Every HTTP request the orchestrator makes to an Ollama server goes
through this class.

Transport errors (``httpx.HTTPError``) are deliberately NOT wrapped: the fleet
manager retries 429/5xx/connect failures and then moves on to the next
candidate node, so it needs the raw exception.

Configuration (env vars, only for the registry singleton
``get_cloud_backend("ollama")``; fleet nodes come from OLLAMA_NODES):
    OLLAMA_URL        — Base URL (default: http://localhost:11434)
    OLLAMA_TIMEOUT_S  — Read timeout in seconds (default: 300)
"""

from __future__ import annotations

import logging
import os

import httpx

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.ollama")

_DEFAULT_EMBED_MODEL = "bge-m3:latest"


class OllamaBackend(BaseAIBackend):
    """A single Ollama server, via its OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        default_model: str | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self._default_model = default_model or os.getenv("OLLAMA_DEFAULT_MODEL", "")
        if client is None:
            timeout = float(os.getenv("OLLAMA_TIMEOUT_S", "300"))
            client = httpx.AsyncClient(base_url=self._base_url, timeout=httpx.Timeout(timeout, connect=5))
        self._client = client

    @property
    def base_url(self) -> str:
        return self._base_url

    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        effective_model = model or self._default_model
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        resp = await self._client.post(
            "/v1/chat/completions",
            json={
                "model": effective_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return text, effective_model, dict(data.get("usage") or {})

    async def embed(self, text: str, model: str = "") -> tuple[list[float], str]:
        effective_model = model or _DEFAULT_EMBED_MODEL
        resp = await self._client.post("/api/embeddings", json={"model": effective_model, "prompt": text})
        resp.raise_for_status()
        return resp.json()["embedding"], effective_model

    async def list_models(self, timeout: float = 5.0) -> list[str]:
        """Names of the models this server has pulled (``GET /api/tags``)."""
        resp = await self._client.get("/api/tags", timeout=timeout)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    async def pull_model(self, model: str, timeout: float = 600.0) -> None:
        resp = await self._client.post("/api/pull", json={"name": model, "stream": False}, timeout=timeout)
        resp.raise_for_status()

    async def health_check(self) -> bool:
        try:
            await self.list_models()
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
