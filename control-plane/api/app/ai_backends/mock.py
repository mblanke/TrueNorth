"""Mock engine adapter: no network, deterministic echo."""

from __future__ import annotations

from .base import BaseAIEngine, empty_probe


class MockEngine(BaseAIEngine):
    def generate(
        self,
        prompt: str,
        *,
        base_url: str = "",
        api_key: str | None = None,
        timeout: float = 30.0,
        model: str = "",
    ) -> dict:
        return {"response": f"[Mock AI] Echo: {prompt[:200]}", "model": "mock-v1"}

    def probe(
        self,
        *,
        base_url: str = "",
        api_key: str | None = None,
        host: str = "",
        port: int = 0,
        timeout: float = 5.0,
    ) -> dict:
        result = empty_probe(base_url or "mock://")
        result["online"] = True
        return result
