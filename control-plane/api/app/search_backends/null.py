"""TrueNorth Range — Null (no-op) search/telemetry backend.

Use when:
  - No search cluster deployed (offline / air-gapped ranges)
  - Integration tests where OpenSearch calls would fail or add noise
  - Feature-flag disabling of telemetry storage

Set SEARCH_BACKEND=null to activate.
"""

from __future__ import annotations

import logging

from .base import BaseSearchBackend

logger = logging.getLogger("truenorth.search.null")


class NullSearchBackend(BaseSearchBackend):
    """Swallows all ingest calls; returns empty results for searches."""

    async def ingest(self, index: str, events: list[dict]) -> int:
        logger.debug("NullSearchBackend: drop %d events for index=%s", len(events), index)
        return 0

    async def search(self, index: str, query: str, size: int = 50) -> dict:
        logger.debug("NullSearchBackend: search index=%s query=%s → empty", index, query)
        return {"hits": {"total": {"value": 0}, "hits": []}}

    async def health_check(self) -> bool:
        return True
