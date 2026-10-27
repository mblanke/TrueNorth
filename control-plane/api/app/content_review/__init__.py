"""The content clock (ADR 0014 decisions 22-24, issue #286).

Importing this package registers its tables on ``Base.metadata``. The feeds (issue #287)
flag golden templates in the worker's sync (``worker/content_feeds``); an API-side section
flags content through ``service.flag_stale``. The rules themselves are in ``staleness`` (pure).
"""

from .models import (
    ContentReviewCycle,
    ContentReviewEdge,
    ContentReviewItem,
    ContentReviewSettings,
    ContentReviewTheme,
)

__all__ = ["ContentReviewCycle", "ContentReviewEdge", "ContentReviewItem", "ContentReviewSettings", "ContentReviewTheme"]
