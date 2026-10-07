"""TrueNorth Range — threat intelligence feed backend registry (ADR 0001).

Usage:
    from app.threat_intel_backends import get_feed_backend

    pull = get_feed_backend(feed.feed_type).fetch(feed.url)              # fetch
    pull = get_feed_backend(feed.feed_type).fetch(None, content=upload)  # uploaded file

The backend is chosen by the feed's own ``feed_type`` column, so feeds of different kinds
live side by side in one tenant.

Supported feed types:
    csv   — CSV with type,value[,first_seen,mitre_technique,...] from a URL or an upload
    null  — fetches nothing (offline ranges, tests)

``taxii``, ``stix_file`` and ``custom_api`` feeds can be recorded but have no backend yet;
pulling one answers 422 naming the types that can be pulled.

Adding a backend:
    1. Create threat_intel_backends/<name>.py implementing BaseFeedBackend
    2. Add an entry to _REGISTRY below (and the feed_type pattern in schemas.ThreatIntelFeedIn)
    3. tests/contracts/test_adapter_contracts.py enrols it automatically
"""

from __future__ import annotations

from .base import (
    BaseFeedBackend,
    FeedError,
    FeedIndicator,
    FeedMalformedError,
    FeedPull,
    FeedRejection,
    FeedSourceError,
    FeedUnreachableError,
)
from .csv_feed import CsvFeedBackend
from .null import NullFeedBackend

__all__ = [
    "BaseFeedBackend",
    "CsvFeedBackend",
    "FeedError",
    "FeedIndicator",
    "FeedMalformedError",
    "FeedPull",
    "FeedRejection",
    "FeedSourceError",
    "FeedUnreachableError",
    "NullFeedBackend",
    "available_feed_types",
    "get_feed_backend",
]

_REGISTRY: dict[str, type[BaseFeedBackend]] = {
    "csv": CsvFeedBackend,
    "null": NullFeedBackend,
}


def available_feed_types() -> list[str]:
    return sorted(_REGISTRY)


def get_feed_backend(feed_type: str) -> BaseFeedBackend:
    """The backend for ``feed_type``. Raises ValueError for a type with no backend."""
    cls = _REGISTRY.get((feed_type or "").strip().lower())
    if cls is None:
        raise ValueError(
            f"No feed backend for feed type {feed_type!r}; feeds that can be pulled: {', '.join(available_feed_types())}"
        )
    return cls()
