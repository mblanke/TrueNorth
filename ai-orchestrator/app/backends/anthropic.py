"""TrueNorth Range AI Orchestrator — Anthropic backend.

Configuration (env vars):
    ANTHROPIC_API_KEY       — API key       (required)
    ANTHROPIC_DEFAULT_MODEL — Default model (default: claude-sonnet-4-20250514)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.anthropic")

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_API_VERSION = "2023-06-01"


class AnthropicBackend(BaseAIBackend):
    """Anthropic Messages API."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._default_model = default_model or os.getenv("ANTHROPIC_DEFAULT_MODEL", _DEFAULT_MODEL)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
            "anthropic-version": _API_VERSION,
        }

    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        effective_model = model or self._default_model
        body: dict = {
            "model": effective_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            body["system"] = system_prompt

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=body,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["content"][0]["text"] if data.get("content") else ""
                return text, effective_model, data.get("usage", {})
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"Anthropic error {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except Exception as exc:
            raise HTTPException(503, f"Anthropic backend unavailable: {exc}") from exc

    async def health_check(self) -> bool:
        # Anthropic has no public /models endpoint; a missing key is the main failure mode
        return bool(self._api_key)
