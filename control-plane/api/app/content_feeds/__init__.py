"""The content feeds (ADR 0014 decision 25, issue #287): release, end-of-life and
vulnerability data that make content due for review on the content clock.

Importing this package registers its tables on ``Base.metadata``. The API's part is the
air-gapped way in (``bundle.py``: verify a signed feed bundle against the configured public
key, then store its entries) and the feeds status (``router.py``). The online pull and the
matching run in the worker (``worker/content_feeds``, ``worker/content_review_sync.py``).
"""

from .models import ContentFeedEntry, ContentFeedFlag, ContentFeedSettings, ContentFeedSource

__all__ = ["ContentFeedEntry", "ContentFeedFlag", "ContentFeedSettings", "ContentFeedSource"]
