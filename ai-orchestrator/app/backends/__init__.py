"""TrueNorth Range AI Orchestrator — AI backend registry and factory.

Usage:
    from app.backends import get_cloud_backend

    backend = get_cloud_backend("openai")
    text, model, usage = await backend.generate(prompt, max_tokens=3000)

Configuration:
    AI_MODEL_BACKEND env var selects the primary backend (default: ollama).

    Supported values for cloud/direct fallback:
        openai    — OpenAI / OpenAI-compatible API
        anthropic — Anthropic Claude
        vllm      — vLLM OpenAI-compatible server (H200 nodes)
        mock      — Deterministic mock (testing / offline)

    The Ollama fleet is managed separately by main.py (multi-node routing,
    health checks, load balancing) and is not included in this registry.

Adding a new backend:
    1. Create ai-orchestrator/app/backends/<name>.py implementing BaseAIBackend
    2. Add an entry to _REGISTRY below
    3. Set AI_MODEL_BACKEND=<name> or reference the backend directly
"""

from __future__ import annotations

import os

from .anthropic import AnthropicBackend
from .base import BaseAIBackend
from .mock import MockAIBackend
from .openai import OpenAIBackend
from .vllm import VLLMBackend

__all__ = [
    "AnthropicBackend",
    "BaseAIBackend",
    "MockAIBackend",
    "OpenAIBackend",
    "VLLMBackend",
    "get_cloud_backend",
    "get_primary_backend",
]

_REGISTRY: dict[str, type[BaseAIBackend]] = {
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
    "vllm": VLLMBackend,
    "mock": MockAIBackend,
}

# Per-type singletons (lazy)
_instances: dict[str, BaseAIBackend] = {}


def get_cloud_backend(name: str) -> BaseAIBackend:
    """Return (or create) a singleton backend instance by name.

    Raises ValueError for unknown names.
    """
    key = name.lower().strip()
    if key not in _instances:
        cls = _REGISTRY.get(key)
        if cls is None:
            valid = ", ".join(sorted(_REGISTRY))
            raise ValueError(
                f"Unknown AI backend={name!r}. Valid options: {valid}"
            )
        _instances[key] = cls()
    return _instances[key]


def get_primary_backend() -> BaseAIBackend:
    """Return the backend for AI_MODEL_BACKEND env var.

    Ollama is the default; if AI_MODEL_BACKEND=ollama (or not set) and
    the Ollama fleet is empty, falls back to 'mock' so the service starts
    cleanly without a GPU cluster.
    """
    name = os.getenv("AI_MODEL_BACKEND", "ollama").lower().strip()
    if name == "ollama":
        # Ollama fleet is managed by main.py; return mock as the named
        # backend so callers that use this factory still get something useful
        name = "mock"
    return get_cloud_backend(name)


def _reset_backends() -> None:  # pragma: no cover — test helper only
    """Clear all cached backend instances.  Call from tests only."""
    _instances.clear()
