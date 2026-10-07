"""TrueNorth Range AI Orchestrator — Abstract AI backend interface.

Every AI backend (OpenAI, Anthropic, vLLM, Ollama, Mock) must implement this
ABC. The Ollama fleet manager in main.py (fleet routing, health checks, load
balancing) is intentionally separate — it orchestrates across one
OllamaBackend per node.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAIBackend(ABC):
    """Minimal interface every AI completion backend must satisfy."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        """Generate a completion for *prompt*.

        Returns:
            (text, model_used, usage_dict)

        Must never raise on recoverable errors — surface them via the
        usage dict or return an empty string with an error key.
        Raises fastapi.HTTPException on unrecoverable failures.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and accepting requests."""
        ...

    async def embed(self, text: str, model: str = "") -> tuple[list[float], str]:
        """Return ``(vector, model_used)`` for *text*.

        Optional capability: backends without an embeddings API keep this
        default, which refuses with 501. Transport failures propagate as
        ``httpx.HTTPError`` so the orchestrator can retry them.
        """
        from fastapi import HTTPException

        raise HTTPException(501, f"{type(self).__name__} does not provide embeddings")
