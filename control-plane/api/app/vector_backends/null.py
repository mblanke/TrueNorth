"""In-memory vector store: offline/air-gapped ranges and tests. Full-text only."""

from __future__ import annotations

from .base import BaseVectorStore


class NullVectorStore(BaseVectorStore):
    def __init__(self) -> None:
        self._indexes: dict[str, tuple[int, list[dict]]] = {}

    async def index_dim(self, index: str) -> int | None:
        entry = self._indexes.get(index)
        return entry[0] if entry else None

    async def create_index(self, index: str, dim: int) -> bool:
        if index in self._indexes:
            return False
        self._indexes[index] = (dim, [])
        return True

    async def bulk_index(self, index: str, docs: list[dict]) -> int:
        if index not in self._indexes:
            return 0
        self._indexes[index][1].extend(docs)
        return len(docs)

    async def search(self, index: str, *, text: str, vector: list[float] | None, k: int) -> list[dict]:
        entry = self._indexes.get(index)
        if not entry:
            return []
        terms = {t for t in text.lower().split() if t}
        scored = []
        for doc in entry[1]:
            score = sum(1 for t in terms if t in doc.get("text", "").lower())
            if score:
                scored.append(
                    {
                        "text": doc["text"],
                        "filename": doc.get("filename", ""),
                        "chunk_ordinal": doc.get("chunk_ordinal", 0),
                        "score": score,
                    }
                )
        return sorted(scored, key=lambda h: -h["score"])[:k]

    async def delete_index(self, index: str) -> None:
        self._indexes.pop(index, None)
