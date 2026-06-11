"""TrueNorth Range — Search/telemetry backend registry and factory.

Usage:
    from app.search_backends import get_search_backend

    backend = get_search_backend()
    count = await backend.ingest(index, events)
    results = await backend.search(index, query)

Configuration:
    SEARCH_BACKEND env var selects the active backend (default: opensearch).

    Supported values:
        opensearch  — OpenSearch 2.x / Elasticsearch 7+ (httpx)
        null        — No-op sink (offline / air-gapped ranges)

Adding a new backend:
    1. Create control-plane/api/app/search_backends/<name>.py implementing BaseSearchBackend
    2. Add an entry to _REGISTRY below
    3. Set SEARCH_BACKEND=<name> in the environment — no other changes required
"""

from __future__ import annotations

import os

from .base import BaseSearchBackend
from .null import NullSearchBackend
from .opensearch import OpenSearchBackend

__all__ = [
    "BaseSearchBackend",
    "NullSearchBackend",
    "OpenSearchBackend",
    "get_search_backend",
]

_REGISTRY: dict[str, type[BaseSearchBackend]] = {
    "opensearch": OpenSearchBackend,
    "null": NullSearchBackend,
}

_instance: BaseSearchBackend | None = None


def get_search_backend() -> BaseSearchBackend:
    """Return the singleton search backend instance.

    Raises ValueError for unknown backend names.
    """
    global _instance
    if _instance is None:
        name = os.getenv("SEARCH_BACKEND", "opensearch").lower().strip()
        cls = _REGISTRY.get(name)
        if cls is None:
            valid = ", ".join(sorted(_REGISTRY))
            raise ValueError(
                f"Unknown SEARCH_BACKEND={name!r}. Valid options: {valid}"
            )
        _instance = cls()
    return _instance


def _reset_backend() -> None:  # pragma: no cover — test helper only
    """Force re-initialisation of the search backend singleton."""
    global _instance
    _instance = None
