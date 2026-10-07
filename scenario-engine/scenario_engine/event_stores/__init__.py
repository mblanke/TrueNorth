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

import os
from typing import Any

from .base import BaseEventStore, EventHits
from .null import NullEventStore
from .opensearch import OpenSearchEventStore

__all__ = [
    "BaseEventStore",
    "EventHits",
    "NullEventStore",
    "OpenSearchEventStore",
    "event_store_from_env",
    "get_event_store",
]

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


def event_store_from_env() -> BaseEventStore:
    """The store a service is deployed against: ``EVENT_STORE`` (default ``opensearch``).

    OpenSearch is reached at ``OPENSEARCH_URL`` with ``OPENSEARCH_USER`` /
    ``OPENSEARCH_PASS`` (the same variables the API's search backend reads), and
    ``OPENSEARCH_VERIFY_SSL`` (``true``, ``false`` or a CA bundle path; default true).
    Services read them here, inside the adapter, rather than building requests themselves.
    """
    name = os.getenv("EVENT_STORE", "opensearch")
    if name == "opensearch":
        return OpenSearchEventStore(
            os.getenv("OPENSEARCH_URL", "http://opensearch:9200"),
            username=os.getenv("OPENSEARCH_USER") or None,
            password=os.getenv("OPENSEARCH_PASS") or None,
            verify_ssl=_verify(os.getenv("OPENSEARCH_VERIFY_SSL", "true")),
        )
    return get_event_store(name)


def _verify(raw: str) -> bool | str:
    flag = raw.strip().lower()
    if flag in ("", "1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return raw.strip()  # a CA bundle path
