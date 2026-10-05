from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventHits:
    total: int
    hits: list[dict[str, Any]] = field(default_factory=list)  # [{"_id": ..., "_source": {...}}]


class BaseEventStore(ABC):
    @abstractmethod
    async def search(self, index: str, query: str | dict[str, Any], size: int = 20) -> EventHits:
        """Events matching ``query`` in ``index`` (a pattern such as ``truenorth-events-*``).

        ``query`` is a Lucene query string or a query DSL dict. Raises on store errors;
        callers decide whether an outage means "not achieved".
        """
