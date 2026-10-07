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

import httpx
from fastapi import HTTPException

from .base import BaseSearchBackend
from .query import ParsedQuery, QueryError, parse_query

logger = logging.getLogger("truenorth.search.opensearch")

# Free text only: no FUZZY, NEAR or SLOP. simple_query_string never raises on bad syntax.
SIMPLE_QUERY_FLAGS = "AND|OR|NOT|PHRASE|PREFIX|PRECEDENCE|WHITESPACE|ESCAPE"


def build_query(parsed: ParsedQuery) -> dict:
    """The OpenSearch query DSL for a parsed telemetry query (never ``query_string``)."""
    if parsed.match_all:
        return {"match_all": {}}
    must: list[dict] = []
    for term in parsed.terms:
        if term.kind == "exists":
            must.append({"exists": {"field": term.field}})
        elif term.kind == "prefix":
            must.append({"prefix": {term.field: {"value": term.value}}})
        else:
            must.append({"match_phrase": {term.field: term.value}})
    if parsed.text:
        must.append(
            {
                "simple_query_string": {
                    "query": parsed.text,
                    "flags": SIMPLE_QUERY_FLAGS,
                    "default_operator": "and",
                    "lenient": True,
                }
            }
        )
    return {"bool": {"must": must}}


class OpenSearchBackend(BaseSearchBackend):
    """OpenSearch / Elasticsearch bulk ingest + constrained search (see query.py)."""

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
        bulk_body = ""
        for event in events:
            bulk_body += json.dumps({"index": {"_index": index}}) + "\n"
            bulk_body += json.dumps(event) + "\n"
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
        try:
            dsl = build_query(parse_query(query))
        except QueryError as exc:
            raise HTTPException(422, f"Invalid search query: {exc}") from exc
        body = {
            "query": dsl,
            "size": size,
            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        }
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(f"{self._url}/{index}/_search", json=body)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:  # e.g. a prefix on a date field
                raise HTTPException(422, "Search query rejected by the search backend") from exc
            raise HTTPException(502, f"OpenSearch error: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"OpenSearch error: {exc}") from exc

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._url}/_cluster/health")
                return resp.status_code < 500
        except Exception:
            return False
