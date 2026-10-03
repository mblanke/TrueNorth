"""Integration test: telemetry ingestion flow.

Send telemetry events via the API, then read them back through the API's own search
endpoint.

The previous version of this file could never have passed: it posted to
`/telemetry/events` and `/telemetry/events/batch`, neither of which exists — the route
is `POST /telemetry/{range_id}/events` and it takes an *array*, so one endpoint covers
both the single and batch cases. It then verified by querying OpenSearch for
`truenorth-telemetry-*`, while ingestion writes to `range-{range_id}` (main.py:344).
On top of that, both verification steps ended in `pytest.skip(...)` on timeout, so even
a total ingestion failure reported as a skip rather than a failure.

Verification goes through `GET /telemetry/{range_id}/search` rather than reaching into
OpenSearch directly: the index naming is an implementation detail, and a test that
encodes it breaks when the backend is swapped.
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 30


def _search(api_client, range_id: str, query: str, expect: int) -> list:
    """Poll the API's search endpoint until `expect` documents match, or fail.

    Ingestion is asynchronous (202 Accepted), so polling is legitimate — but running
    out of time is a failure, not a skip. A silent skip here is what let the whole
    telemetry path rot unnoticed.
    """
    deadline = time.time() + POLL_TIMEOUT
    last = 0
    while time.time() < deadline:
        resp = api_client.get(f"/telemetry/{range_id}/search", params={"q": query, "size": 50})
        if resp.status_code == 200:
            hits = resp.json()
            docs = hits.get("hits", hits) if isinstance(hits, dict) else hits
            if isinstance(docs, dict):
                docs = docs.get("hits", [])
            last = len(docs)
            if last >= expect:
                return docs
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"telemetry search for {query!r} on range {range_id} returned {last} docs, "
        f"expected >= {expect}, within {POLL_TIMEOUT}s"
    )


@pytest.fixture(scope="class")
def telemetry_range(request, api_client, range_template):
    """Telemetry is indexed per range (`range-{range_id}`), so it needs a real one."""
    resp = api_client.post(
        "/ranges", json={"name": "integ-telemetry", "template_id": range_template}
    )
    assert resp.status_code in (200, 201), f"POST /ranges => {resp.status_code} {resp.text}"
    range_id = resp.json()["id"]
    yield range_id
    api_client.delete(f"/ranges/{range_id}")


class TestTelemetryFlow:
    """Verify telemetry events flow from the API into the search backend."""

    def test_send_single_event(self, api_client, telemetry_range):
        event_id = str(uuid.uuid4())
        resp = api_client.post(
            f"/telemetry/{telemetry_range}/events",
            json=[
                {
                    "event_id": event_id,
                    "source": "integration-test",
                    "event_type": "test.single",
                    "data": {"key": "value", "test": True},
                }
            ],
        )
        assert resp.status_code in (200, 201, 202), resp.text
        assert resp.json()["accepted"] == 1
        self.__class__._single_event_id = event_id

    def test_send_batch_events(self, api_client, telemetry_range):
        """The same endpoint is the batch endpoint — it takes an array."""
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
        resp = api_client.post(f"/telemetry/{telemetry_range}/events", json=events)
        assert resp.status_code in (200, 201, 202), resp.text
        assert resp.json()["accepted"] == 10
        self.__class__._batch_tag = batch_tag

    def test_verify_single_event_is_searchable(self, api_client, telemetry_range):
        event_id = self.__class__._single_event_id
        docs = _search(api_client, telemetry_range, f"event_id:{event_id}", expect=1)
        assert docs

    def test_verify_batch_is_searchable(self, api_client, telemetry_range):
        batch_tag = self.__class__._batch_tag
        _search(api_client, telemetry_range, f"data.batch_tag:{batch_tag}", expect=10)

    def test_events_are_scoped_to_their_range(self, api_client, telemetry_range, range_template):
        """Telemetry is indexed per range, so another range must not see these events."""
        other = api_client.post(
            "/ranges", json={"name": "integ-telemetry-other", "template_id": range_template}
        )
        assert other.status_code in (200, 201), other.text
        other_id = other.json()["id"]
        try:
            # Give the other range an index of its own first. Searching a range that has
            # never ingested anything returns 502 (the backend index does not exist),
            # which would make this pass for the wrong reason.
            seeded = api_client.post(
                f"/telemetry/{other_id}/events",
                json=[{
                    "event_id": str(uuid.uuid4()),
                    "source": "integration-test",
                    "event_type": "test.other",
                    "data": {"key": "other"},
                }],
            )
            assert seeded.status_code in (200, 201, 202), seeded.text
            _search(api_client, other_id, "event_type:test.other", expect=1)

            resp = api_client.get(
                f"/telemetry/{other_id}/search",
                params={"q": f"event_id:{self.__class__._single_event_id}", "size": 50},
            )
            assert resp.status_code == 200
            payload = resp.json()
            docs = payload.get("hits", payload) if isinstance(payload, dict) else payload
            if isinstance(docs, dict):
                docs = docs.get("hits", [])
            assert not docs
        finally:
            api_client.delete(f"/ranges/{other_id}")
