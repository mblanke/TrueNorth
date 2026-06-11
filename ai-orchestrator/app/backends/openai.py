"""TrueNorth Range AI Orchestrator — OpenAI backend.

Supports OpenAI and any OpenAI-compatible endpoint (Azure OpenAI,
Together AI, Groq, etc.) by setting OPENAI_BASE_URL.

Configuration (env vars):
    OPENAI_API_KEY   — API key     (required unless using Azure MSI)
    OPENAI_BASE_URL  — Base URL    (default: https://api.openai.com/v1)
    OPENAI_DEFAULT_MODEL — Default model (default: gpt-4o)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.openai")

_DEFAULT_MODEL = "gpt-4o"


class OpenAIBackend(BaseAIBackend):
    """OpenAI Chat Completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self._default_model = default_model or os.getenv("OPENAI_DEFAULT_MODEL", _DEFAULT_MODEL)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

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

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json={"model": effective_model, "messages": messages, "max_tokens": max_tokens},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return text, effective_model, data.get("usage", {})
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"OpenAI error {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except Exception as exc:
            raise HTTPException(503, f"OpenAI backend unavailable: {exc}") from exc

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/models",
                    headers=self._headers(),
                )
                return resp.status_code < 500
        except Exception:
            return False
