import logging
from typing import Any
from .base import BaseValidator

logger = logging.getLogger(__name__)


class OpenSearchQueryValidator(BaseValidator):
    def validate(self, context: dict[str, Any]) -> bool:
        query = self.params.get("query", "*")
        min_hits = int(self.params.get("min_hits", 1))
        client = context.get("opensearch_client")
        if client is None:
            logger.warning("No OpenSearch client in context")
            return False
        try:
            resp = client.search(index="truenorth-events-*", body={"query": {"query_string": {"query": query}}})
            total = resp["hits"]["total"]["value"]
            return total >= min_hits
        except Exception as exc:
            logger.error("OpenSearch query failed: %s", exc)
            return False