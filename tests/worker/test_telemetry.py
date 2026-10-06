"""Worker ingest: server-stamped ingest time and the store's credentials (ADR 0005, review finding 2)."""

from __future__ import annotations

import base64
import json

import httpx
import respx
from worker import telemetry

OS = "http://os.test:9200"


def test_stamp_overwrites_any_truenorth_field_the_sender_sent():
    forged = {"x": 1, "truenorth": {"ingested_at": "1999"}, "truenorth.ingested_at": "1999", "@timestamp": "1999"}
    out = telemetry.stamp(forged, "r-1", "2026-10-06T12:00:00+00:00")
    assert out["truenorth"] == {"ingested_at": "2026-10-06T12:00:00+00:00", "range_id": "r-1"}
    assert "truenorth.ingested_at" not in out
    assert out["@timestamp"] == "1999" and out["x"] == 1


@respx.mock
def test_bulk_ingest_sends_basic_auth_and_stamps(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", OS)
    monkeypatch.setenv("OPENSEARCH_USER", "ingest")
    monkeypatch.setenv("OPENSEARCH_PASS", "pw")
    route = respx.post(f"{OS}/_bulk").mock(return_value=httpx.Response(200, json={"errors": False, "items": []}))
    assert telemetry.bulk_ingest("r-1", [{"a": 1}]) == 1
    req = route.calls.last.request
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"ingest:pw").decode()
    action, doc = (json.loads(line) for line in req.content.decode().splitlines())
    assert action == {"index": {"_index": "range-r-1"}}
    assert doc["truenorth"]["range_id"] == "r-1" and doc["truenorth"]["ingested_at"]


@respx.mock
def test_bulk_ingest_without_user_sends_no_auth(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", OS)
    monkeypatch.delenv("OPENSEARCH_USER", raising=False)
    route = respx.post(f"{OS}/_bulk").mock(return_value=httpx.Response(200, json={"errors": False, "items": []}))
    telemetry.bulk_ingest("r-1", [{"a": 1}])
    assert "authorization" not in route.calls.last.request.headers
