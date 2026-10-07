"""OpenSearch (or Elasticsearch 7+) event store over plain HTTP."""

from __future__ import annotations

from typing import Any

import httpx

from .base import BaseEventStore, EventHits


class OpenSearchEventStore(BaseEventStore):
    """``username``/``password`` are HTTP basic auth (production runs the security plugin).

    ``verify_ssl`` is True or a CA bundle path; it matters only for an ``https`` URL.
    """

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool | str = True,
        timeout: float = 10.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.auth = (username, password or "") if username else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    async def search(self, index: str, query: str | dict[str, Any], size: int = 20) -> EventHits:
        dsl = {"query_string": {"query": query}} if isinstance(query, str) else query
        payload = {"query": dsl, "size": size, "track_total_hits": True}
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout, auth=self.auth) as client:
            # A range's index does not exist until its first event is ingested; that is
            # "no matches", not a store failure.
            resp = await client.post(f"{self.url}/{index}/_search", params={"ignore_unavailable": "true"}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        hits = data.get("hits", {})
        total = hits.get("total", 0)
        total = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
        return EventHits(
            total=total,
            hits=[{"_id": h.get("_id"), "_source": h.get("_source", {})} for h in hits.get("hits", [])],
        )
