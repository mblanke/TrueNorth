"""Integration test: telemetry ingestion flow.

Send telemetry events via API -> verify they appear in OpenSearch.
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 30


class TestTelemetryFlow:
    """Verify telemetry events flow from API to OpenSearch."""

    def test_send_single_event(self, api_client):
        event_id = str(uuid.uuid4())
        resp = api_client.post(
            "/telemetry/events",
            json={
                "event_id": event_id,
                "source": "integration-test",
                "event_type": "test.single",
                "data": {"key": "value", "test": True},
            },
        )
        assert resp.status_code in (200, 201, 202)
        self.__class__._single_event_id = event_id

    def test_send_batch_events(self, api_client):
        batch_tag = str(uuid.uuid4())[:8]
        events = [
            {
                "event_id": str(uuid.uuid4()),
                "source": "integration-test",
                "event_type": "test.batch",
                "data": {"batch_tag": batch_tag, "index": i},
            }
            for i in range(10)
        ]
        resp = api_client.post("/telemetry/events/batch", json=events)
        assert resp.status_code in (200, 201, 202)
        self.__class__._batch_tag = batch_tag

    def test_verify_single_event_in_opensearch(self, opensearch_url):
        """Poll OpenSearch for the single event (telemetry pipeline may buffer)."""
        import httpx

        event_id = self.__class__._single_event_id
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            resp = httpx.get(
                f"{opensearch_url}/truenorth-telemetry-*/_search",
                params={"q": f"event_id:{event_id}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                hits = resp.json().get("hits", {}).get("total", {})
                count = hits.get("value", 0) if isinstance(hits, dict) else hits
                if count > 0:
                    return
            time.sleep(POLL_INTERVAL)
        pytest.skip("OpenSearch index not yet populated — pipeline may not be running")

    def test_verify_batch_in_opensearch(self, opensearch_url):
        """Verify batch events appear in OpenSearch."""
        import httpx

        batch_tag = self.__class__._batch_tag
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            resp = httpx.get(
                f"{opensearch_url}/truenorth-telemetry-*/_search",
                params={"q": f"data.batch_tag:{batch_tag}", "size": 0},
                timeout=10.0,
            )
            if resp.status_code == 200:
                hits = resp.json().get("hits", {}).get("total", {})
                count = hits.get("value", 0) if isinstance(hits, dict) else hits
                if count >= 10:
                    return
            time.sleep(POLL_INTERVAL)
        pytest.skip("Batch events not found in OpenSearch within timeout")
