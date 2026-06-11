"""TrueNorth Range AI Orchestrator — Mock AI backend.

Returns deterministic fake responses for:
  - Unit testing without GPU/API access
  - CI pipelines where cloud keys are unavailable
  - Offline ranges without internet access

Set AI_MODEL_BACKEND=mock to activate.
"""

from __future__ import annotations

import logging

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.mock")


class MockAIBackend(BaseAIBackend):
    """Deterministic mock backend — no external calls."""

    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        effective_model = model or "mock"
        response = f"[MOCK] Response for: {prompt[:200]}{'...' if len(prompt) > 200 else ''}"
        usage = {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": 10,
            "total_tokens": len(prompt.split()) + 10,
        }
        logger.debug("MockAIBackend.generate: model=%s tokens=%d", effective_model, usage["total_tokens"])
        return response, effective_model, usage

    async def health_check(self) -> bool:
        return True
