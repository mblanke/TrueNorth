"""TrueNorth Range — Abstract search/telemetry backend interface.

Abstracts bulk event ingestion and free-text search so the control plane
can swap between OpenSearch, Elasticsearch, or a null sink without
changing call sites.

Swapping backends is an env-var change (SEARCH_BACKEND=<name>).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSearchBackend(ABC):
    """Minimal interface every search/telemetry backend must satisfy."""

    @abstractmethod
    async def ingest(self, index: str, events: list[dict]) -> int:
        """Bulk-ingest *events* into *index*.

        Returns the count of events accepted.  Must never raise — return 0
        on failure so telemetry ingestion never blocks API responses.
        """
        ...

    @abstractmethod
    async def search(self, index: str, query: str, size: int = 50) -> dict:
        """Search *index* with a query in the telemetry grammar (``query.parse_query``).

        Never pass *query* to a backend's own free-form parser (OpenSearch
        ``query_string``): build the request from ``parse_query(query)``.
        Returns the raw search response dict (hits, total, etc.).
        Raises fastapi.HTTPException(422) for a query outside the grammar and
        HTTPException(502) on backend errors.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable."""
        ...
