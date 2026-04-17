"""Integration test: full range lifecycle.

create -> provision -> health check -> snapshot -> restore -> destroy
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2  # seconds
POLL_TIMEOUT = 120  # seconds


def _poll_range_state(client, range_id: str, target: str) -> dict:
    """Poll GET /ranges/{id} until state == target or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}")
        assert resp.status_code == 200, f"GET /ranges/{range_id} => {resp.status_code}"
        data = resp.json()
        if data["state"] == target:
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Range {range_id} did not reach state '{target}' within {POLL_TIMEOUT}s")


class TestRangeLifecycle:
    """End-to-end range lifecycle against live services."""

    def test_create_range(self, api_client):
        resp = api_client.post(
            "/ranges",
            json={
                "name": "integ-range-lifecycle",
                "template_id": None,
                "description": "Integration test range",
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "id" in data
        self.__class__._range_id = data["id"]

    def test_provision_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.post(f"/ranges/{range_id}/provision")
        assert resp.status_code in (200, 202)
        _poll_range_state(api_client, range_id, "provisioned")

    def test_health_check(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("healthy", "ok")

    def test_snapshot_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.post(f"/ranges/{range_id}/snapshot")
        assert resp.status_code in (200, 202)
        # Poll until snapshot completes
        _poll_range_state(api_client, range_id, "provisioned")

    def test_restore_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.post(f"/ranges/{range_id}/restore")
        assert resp.status_code in (200, 202)
        _poll_range_state(api_client, range_id, "provisioned")

    def test_destroy_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.delete(f"/ranges/{range_id}")
        assert resp.status_code in (200, 202, 204)
        _poll_range_state(api_client, range_id, "destroyed")
