"""TrueNorth Range — null threat intelligence feed backend.

The offline backend every seam carries (ADR 0001 rule 4): it fetches nothing and returns
no indicators, so a feed of this type can be pulled on an air-gapped range or in a test
without touching the network.
"""

from __future__ import annotations

from .base import BaseFeedBackend, FeedPull


class NullFeedBackend(BaseFeedBackend):
    accepts_upload = True

    def fetch(self, url: str | None, content: bytes | None = None) -> FeedPull:
        return FeedPull()
