"""TrueNorth Range — OpenSearch search/telemetry backend.

Works with OpenSearch 2.x and Elasticsearch 7/8 (compatible APIs).

Configuration (env vars):
    OPENSEARCH_URL  — base URL  (default: http://opensearch:9200)
    OPENSEARCH_USER — HTTP basic auth username (optional)
    OPENSEARCH_PASS — HTTP basic auth password (optional)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from .base import BaseSearchBackend, SearchBackendError, SearchMatch, SearchQueryError

logger = logging.getLogger("truenorth.search.opensearch")


def _reason(resp: httpx.Response) -> str:
    """The parse error OpenSearch gave, without index names or stack traces."""
    try:
        err = resp.json().get("error", {})
        causes = err.get("root_cause") or [err]
        return str(causes[0].get("reason", "query could not be parsed"))[:300]
    except Exception:  # noqa: BLE001
        return "query could not be parsed"


def stamp_ingested(event: dict, now: str) -> dict:
    """The event as stored, with ``truenorth.ingested_at`` set from this server's clock.

    Detection credit matches the exercise window on this field, never on the sender's
    ``@timestamp`` (ADR 0005). Any ``truenorth`` field the sender supplied is dropped first,
    so no sender can place an event inside a window.
    """
    out = {k: v for k, v in event.items() if k != "truenorth" and not k.startswith("truenorth.")}
    out["truenorth"] = {"ingested_at": now}
    return out


class OpenSearchBackend(BaseSearchBackend):
    """OpenSearch / Elasticsearch bulk ingest + query-string search."""

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._url = (url or os.getenv("OPENSEARCH_URL", "http://opensearch:9200")).rstrip("/")
        _user = username or os.getenv("OPENSEARCH_USER", "")
        _pass = password or os.getenv("OPENSEARCH_PASS", "")
        self._auth: tuple[str, str] | None = (_user, _pass) if _user else None

    def _client_kwargs(self) -> dict:
        kwargs: dict = {"timeout": 10.0}
        if self._auth:
            kwargs["auth"] = self._auth
        return kwargs

    # ------------------------------------------------------------------
    # BaseSearchBackend implementation
    # ------------------------------------------------------------------

    async def ingest(self, index: str, events: list[dict]) -> int:
        if not events:
            return 0
        now = datetime.now(UTC).isoformat()
        bulk_body = ""
        for event in events:
            bulk_body += json.dumps({"index": {"_index": index}}) + "\n"
            bulk_body += json.dumps(stamp_ingested(event, now), default=str) + "\n"
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(
                    f"{self._url}/_bulk",
                    content=bulk_body,
                    headers={"Content-Type": "application/x-ndjson"},
                )
                resp.raise_for_status()
                # Count items with no error in the bulk response
                result = resp.json()
                items = result.get("items", [])
                return sum(
                    1 for item in items
                    if item.get("index", {}).get("status", 500) < 300
                )
        except Exception as exc:
            logger.error("OpenSearch ingest error: %s", exc)
            return 0

    async def search(self, index: str, query: str, size: int = 50) -> dict:
        body = {
            "query": {"query_string": {"query": query}},
            "size": size,
            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        }
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(f"{self._url}/{index}/_search", json=body)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"OpenSearch error: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"OpenSearch error: {exc}") from exc

    async def match(self, index: str, query: dict, size: int = 0) -> SearchMatch:
        # The server gives up (timed_out) before the client does, so a slow query is reported
        # as such rather than as a dropped connection.
        body = {"query": query, "size": size, "track_total_hits": True, "_source": False, "timeout": "5s"}
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(
                    f"{self._url}/{index}/_search", params={"ignore_unavailable": "true"}, json=body
                )
        except Exception as exc:
            raise SearchBackendError(f"OpenSearch error: {exc}") from exc
        if resp.status_code == 400:
            raise SearchQueryError(_reason(resp))
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SearchBackendError(f"OpenSearch error: {exc}") from exc
        if data.get("timed_out"):
            raise SearchBackendError("OpenSearch query timed out")
        hits = data.get("hits", {})
        total = hits.get("total", 0)
        total = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
        return SearchMatch(total=total, ids=[str(h.get("_id")) for h in hits.get("hits", [])])

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._url}/_cluster/health")
                return resp.status_code < 500
        except Exception:
            return False
