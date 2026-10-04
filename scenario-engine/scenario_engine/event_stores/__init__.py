"""Event stores that detection objectives are scored against (docs/adr/0001).

Scenario content writes detection queries in Lucene ``query_string`` syntax, which
OpenSearch and Elasticsearch both execute; a query may also be a DSL dict. Validators
ask a ``BaseEventStore`` for matches and never talk to a search engine themselves, so
a different store (another engine, a translator, an offline fixture) is one new class
plus one registry line.

    store = get_event_store("opensearch", url="https://opensearch:9200")
    hits = await store.search("truenorth-events-*", 'process_name:psexec.exe', size=10)
"""

from __future__ import annotations

from typing import Any

from .base import BaseEventStore, EventHits
from .null import NullEventStore
from .opensearch import OpenSearchEventStore

__all__ = ["BaseEventStore", "EventHits", "NullEventStore", "OpenSearchEventStore", "get_event_store"]

_REGISTRY: dict[str, type[BaseEventStore]] = {
    "opensearch": OpenSearchEventStore,
    "null": NullEventStore,
}


def get_event_store(name: str = "opensearch", **kwargs: Any) -> BaseEventStore:
    """Build the named store. Raises ValueError for an unknown name."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown event store {name!r}; expected one of {sorted(_REGISTRY)}")
    return cls(**kwargs)
