import logging
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class HttpBurstInjector(BaseInjector):
    name: str = "http_burst"
    description: str = "Builds synthetic records of a burst of HTTP requests; sends no requests."
    required_params: list[str] = ["url"]

    def validate_params(self) -> None:
        assert "url" in self.params or "target_url" in self.params

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        url = self.params.get("url") or self.params["target_url"]
        count = int(self.params.get("count", 10))
        method = self.params.get("method", "GET").upper()
        results = [{"url": url, "method": method, "seq": i} for i in range(count)]
        return {"injector": "http_burst", "url": url, "total_requests": count, "results": results}
