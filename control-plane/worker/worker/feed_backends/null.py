"""A feed source that fetches nothing (offline sites, tests)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .base import BaseFeedSource, FeedProduct, FeedResult


class NullFeedSource(BaseFeedSource):
    name = "null"
    label = "No feed"

    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None, now: datetime) -> FeedResult:
        return FeedResult()
