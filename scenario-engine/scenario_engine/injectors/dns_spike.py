import logging
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class DnsSpikeInjector(BaseInjector):
    name: str = "dns_spike"
    description: str = "Generates a burst of DNS queries against the given domains."
    required_params: list[str] = ["domains", "count"]

    def validate_params(self) -> None:
        assert "domains" in self.params
        assert "count" in self.params

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        domains = self.params["domains"]
        count = int(self.params.get("count", 100))
        results = [{"query": domains[i % len(domains)], "seq": i} for i in range(count)]
        return {"injector": "dns_spike", "total_queries": count, "results": results}
