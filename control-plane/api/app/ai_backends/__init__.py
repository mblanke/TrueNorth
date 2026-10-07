"""TrueNorth Range — AI inference engine adapter registry (ADR 0001).

Usage:
    from app.ai_backends import get_ai_engine

    engine = get_ai_engine(backend.backend_type)
    result = engine.generate(prompt, base_url=backend.base_url, api_key=..., timeout=...)

``backend_type`` is the ``AIBackendConfig.backend_type`` column:
    ollama                                           — Ollama (fleet nodes)
    openai, azure_openai, vllm, litellm, anthropic   — OpenAI-compatible API
                                                       (anthropic is reached through
                                                       the LiteLLM router)
    mock                                             — no network

Adding an engine: implement BaseAIEngine in ai_backends/<name>.py and add its
backend_type(s) to _REGISTRY. Routers do not change.
"""

from __future__ import annotations

from .base import BaseAIEngine, empty_probe
from .mock import MockEngine
from .ollama import OllamaEngine
from .openai_compat import OpenAICompatibleEngine

__all__ = [
    "BaseAIEngine",
    "MockEngine",
    "OllamaEngine",
    "OpenAICompatibleEngine",
    "empty_probe",
    "get_ai_engine",
    "known_engine_types",
]

_OPENAI_COMPATIBLE = OpenAICompatibleEngine()

_REGISTRY: dict[str, BaseAIEngine] = {
    "ollama": OllamaEngine(),
    "openai": _OPENAI_COMPATIBLE,
    "azure_openai": _OPENAI_COMPATIBLE,
    "vllm": _OPENAI_COMPATIBLE,
    "litellm": _OPENAI_COMPATIBLE,
    "anthropic": _OPENAI_COMPATIBLE,
    "mock": MockEngine(),
}


def known_engine_types() -> list[str]:
    return sorted(_REGISTRY)


def get_ai_engine(backend_type: str, default: str | None = None) -> BaseAIEngine:
    """Adapter for *backend_type*.

    Unknown types raise ValueError, unless *default* names a registered type to
    use instead (the admin screen accepts free-text types, so callers that must
    always answer pass one).
    """
    engine = _REGISTRY.get((backend_type or "").lower().strip())
    if engine is not None:
        return engine
    if default is not None:
        return _REGISTRY[default]
    raise ValueError(f"Unknown AI backend type {backend_type!r}; expected one of {known_engine_types()}")
