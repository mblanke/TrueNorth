"""OpenSearch query validator — checks for matching events in telemetry."""
from __future__ import annotations

import json
import logging
import os

import httpx

from . import ValidationResult, Validator, register_validator

logger = logging.getLogger(__name__)


@register_validator
class OpenSearchQueryValidator(Validator):
    validator_name = "opensearch_query"

    def check(self, params: dict, range_id: str, tenant_id: str) -> ValidationResult:
        query = params.get("query", "*")
        min_hits = params.get("min_hits", 1)
        index = params.get("index", f"range-{range_id}")
        os_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")

        try:
            body = {"query": {"query_string": {"query": query}}, "size": 0}
            resp = httpx.post(f"{os_url}/{index}/_search", json=body)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            passed = total >= min_hits

            return ValidationResult(
                passed=passed,
                validator_name="opensearch_query",
                evidence=f"Found {total} hits (threshold: {min_hits})",
                detail=json.dumps({"query": query, "index": index, "total": total}),
            )
        except Exception as e:
            return ValidationResult(
                passed=False,
                validator_name="opensearch_query",
                evidence=f"Error querying OpenSearch: {e}",
            )
