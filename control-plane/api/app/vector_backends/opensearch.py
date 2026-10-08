"""OpenSearch k-NN vector store (lucene HNSW, cosine)."""

from __future__ import annotations

import json
import logging

import httpx

from ..search_backends import connection
from .base import BaseVectorStore

logger = logging.getLogger("truenorth.api.vector")

_SOURCE = ["text", "filename", "chunk_ordinal"]


class OpenSearchVectorStore(BaseVectorStore):
    """The same store, URL, credentials and TLS settings as the search backend
    (search_backends/connection.py)."""

    def __init__(self, url: str | None = None) -> None:
        self.url = (url or connection.opensearch_url()).rstrip("/")

    async def index_dim(self, index: str) -> int | None:
        try:
            async with httpx.AsyncClient(**connection.client_kwargs(15)) as client:
                resp = await client.get(f"{self.url}/{index}/_mapping")
            if resp.status_code != 200:
                return None
            props = resp.json()[index]["mappings"]["properties"]
            return int(props["embedding"]["dimension"])
        except Exception as exc:
            logger.debug("Could not read the mapping for %s (%s)", index, exc)
            return None

    async def create_index(self, index: str, dim: int) -> bool:
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "curriculum_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "filename": {"type": "keyword"},
                    "chunk_ordinal": {"type": "integer"},
                    "text": {"type": "text"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dim,
                        "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
                    },
                }
            },
        }
        async with httpx.AsyncClient(**connection.client_kwargs(15)) as client:
            resp = await client.put(f"{self.url}/{index}", json=mapping)
        if resp.status_code < 300:
            return True
        # Lost a race with a concurrent create: fine if the index now exists.
        if await self.index_dim(index) is None:
            resp.raise_for_status()
        return False

    async def bulk_index(self, index: str, docs: list[dict]) -> int:
        if not docs:
            return 0
        lines: list[str] = []
        for doc in docs:
            lines.append(json.dumps({"index": {"_index": index}}))
            lines.append(json.dumps(doc))
        async with httpx.AsyncClient(**connection.client_kwargs(60)) as client:
            resp = await client.post(
                f"{self.url}/_bulk",
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            resp.raise_for_status()
            result = resp.json()
        return sum(1 for item in result.get("items", []) if item.get("index", {}).get("status", 500) < 300)

    async def search(self, index: str, *, text: str, vector: list[float] | None, k: int) -> list[dict]:
        if vector:
            query: dict = {"knn": {"embedding": {"vector": vector, "k": k}}}
        else:
            query = {"match": {"text": {"query": text}}}
        body = {"size": k, "query": query, "_source": _SOURCE}
        async with httpx.AsyncClient(**connection.client_kwargs(30)) as client:
            resp = await client.post(f"{self.url}/{index}/_search", json=body)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
        return [
            {
                "text": h["_source"]["text"],
                "filename": h["_source"].get("filename", ""),
                "chunk_ordinal": h["_source"].get("chunk_ordinal", 0),
                "score": h.get("_score", 0),
            }
            for h in hits
        ]

    async def delete_index(self, index: str) -> None:
        async with httpx.AsyncClient(**connection.client_kwargs(15)) as client:
            await client.delete(f"{self.url}/{index}")
