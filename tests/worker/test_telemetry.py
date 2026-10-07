"""Worker ingest: server-stamped ingest time and the store's credentials (ADR 0005, review finding 2).

worker/telemetry.py holds the pieces; telemetry_tasks.ingest_telemetry_batch is the one writer.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from worker import telemetry

OS = "http://os.test:9200"


def test_stamp_overwrites_any_truenorth_field_the_sender_sent():
    forged = {"x": 1, "truenorth": {"ingested_at": "1999"}, "truenorth.ingested_at": "1999", "@timestamp": "1999"}
    out = telemetry.stamp(forged, "r-1", "2026-10-06T12:00:00+00:00")
    assert out["truenorth"] == {"ingested_at": "2026-10-06T12:00:00+00:00", "range_id": "r-1"}
    assert "truenorth.ingested_at" not in out
    assert out["@timestamp"] == "1999" and out["x"] == 1


@pytest.mark.parametrize(
    ("verify", "expected"),
    [("true", None), ("", None), ("false", False), ("off", False), ("/etc/ssl/ca.pem", "/etc/ssl/ca.pem")],
)
def test_client_kwargs_reads_tls_verification(monkeypatch, verify, expected):
    monkeypatch.setenv("OPENSEARCH_VERIFY_SSL", verify)
    monkeypatch.delenv("OPENSEARCH_USER", raising=False)
    kwargs = telemetry.client_kwargs(timeout=5)
    assert kwargs["timeout"] == 5 and kwargs.get("verify") == expected and "auth" not in kwargs


class TestIngestTask:
    @pytest.fixture(autouse=True)
    def _store(self, monkeypatch):
        pytest.importorskip("celery")
        monkeypatch.setenv("OPENSEARCH_URL", OS)

    def _ingest(self, respx_mock, events):
        from worker import telemetry_tasks

        route = respx_mock.post(f"{OS}/_bulk").mock(
            return_value=httpx.Response(200, json={"errors": False, "items": []})
        )
        telemetry_tasks.ingest_telemetry_batch("r-1", events)
        return route.calls.last.request

    def test_every_event_carries_the_servers_ingest_time_not_the_senders(self, respx_mock, monkeypatch):
        monkeypatch.delenv("OPENSEARCH_USER", raising=False)
        req = self._ingest(respx_mock, [{"a": 1, "truenorth": {"ingested_at": "1999-01-01T00:00:00Z"}}])
        action, doc = (json.loads(line) for line in req.content.decode().splitlines())
        assert action == {"index": {"_index": "range-r-1"}}
        assert doc["truenorth"]["range_id"] == "r-1"
        assert doc["truenorth"]["ingested_at"] > "2026"  # forged 1999 value replaced

    def test_basic_auth_when_a_store_user_is_set(self, respx_mock, monkeypatch):
        monkeypatch.setenv("OPENSEARCH_USER", "ingest")
        monkeypatch.setenv("OPENSEARCH_PASS", "pw")
        req = self._ingest(respx_mock, [{"a": 1}])
        assert req.headers["authorization"] == "Basic " + base64.b64encode(b"ingest:pw").decode()

    def test_no_auth_without_a_store_user(self, respx_mock, monkeypatch):
        monkeypatch.delenv("OPENSEARCH_USER", raising=False)
        req = self._ingest(respx_mock, [{"a": 1}])
        assert "authorization" not in req.headers
