"""TrueNorth Range — vector store registry (curriculum RAG).

Usage:
    from app.vector_backends import get_vector_store

    store = get_vector_store()
    hits = await store.search(index, text=query, vector=None, k=8)

Configuration:
    VECTOR_BACKEND selects the store (default: opensearch).
        opensearch  — OpenSearch k-NN (OPENSEARCH_URL)
        null        — in-memory, full-text only (offline ranges, tests)

Adding a backend: implement BaseVectorStore in vector_backends/<name>.py and add it to
_REGISTRY. Nothing else changes.
"""

from __future__ import annotations

import os

from .base import BaseVectorStore
from .null import NullVectorStore
from .opensearch import OpenSearchVectorStore

__all__ = ["BaseVectorStore", "NullVectorStore", "OpenSearchVectorStore", "get_vector_store"]

_REGISTRY: dict[str, type[BaseVectorStore]] = {
    "opensearch": OpenSearchVectorStore,
    "null": NullVectorStore,
}

_instance: BaseVectorStore | None = None


def get_vector_store() -> BaseVectorStore:
    """Singleton store for VECTOR_BACKEND. Raises ValueError for unknown names."""
    global _instance
    if _instance is None:
        name = os.getenv("VECTOR_BACKEND", "opensearch").lower()
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown VECTOR_BACKEND {name!r}; expected one of {sorted(_REGISTRY)}")
        _instance = cls()
    return _instance


def reset_vector_store() -> None:
    """Drop the singleton (tests, or after changing VECTOR_BACKEND)."""
    global _instance
    _instance = None
