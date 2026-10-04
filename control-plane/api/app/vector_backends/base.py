"""Vector store interface for curriculum RAG (docs/adr/0001-adapter-registry.md).

Distinct from ``search_backends`` (telemetry ingest and Lucene search): this is a
per-curriculum index of text chunks with an optional fixed-width embedding, searched
by kNN when a query vector fits and by full-text otherwise. Width resolution policy
stays in ``curriculum_ingest``; a backend only stores and retrieves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseVectorStore(ABC):
    @abstractmethod
    async def index_dim(self, index: str) -> int | None:
        """Embedding width fixed in an existing index, or None if it does not exist."""

    @abstractmethod
    async def create_index(self, index: str, dim: int) -> bool:
        """Create the index for ``dim``-wide vectors. False if it already existed."""

    @abstractmethod
    async def bulk_index(self, index: str, docs: list[dict]) -> int:
        """Store chunk documents (``embedding`` key optional). Returns how many succeeded."""

    @abstractmethod
    async def search(self, index: str, *, text: str, vector: list[float] | None, k: int) -> list[dict]:
        """Top-k hits as ``{"text", "filename", "chunk_ordinal", "score"}``.

        kNN on ``vector`` when given, full-text on ``text`` otherwise. A missing
        index returns [] rather than raising.
        """

    @abstractmethod
    async def delete_index(self, index: str) -> None:
        """Drop the index; missing is not an error."""
