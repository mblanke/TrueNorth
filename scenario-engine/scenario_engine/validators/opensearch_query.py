"""Detection objective: enough events match a query in the range's event store.

Named ``opensearch_query`` because scenario content uses that validator name; the store
itself comes from ``context["event_store"]`` (any ``scenario_engine.event_stores``
backend), so the content is not tied to OpenSearch.
"""

import asyncio
import logging
from typing import Any

from ..event_stores import BaseEventStore
from .base import BaseValidator

logger = logging.getLogger(__name__)

DEFAULT_INDEX = "truenorth-events-*"


class OpenSearchQueryValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        query = self.params.get("query", "*")
        min_hits = int(self.params.get("min_hits", 1))
        index = self.params.get("index", DEFAULT_INDEX)
        store: BaseEventStore | None = context.get("event_store")
        if store is None:
            logger.warning("No event store in context")
            return False
        try:
            found = asyncio.run(store.search(index, query, size=1))
        except Exception as exc:  # includes being called inside a running loop
            logger.error("Event store query failed: %s", exc)
            return False
        return found.total >= min_hits
