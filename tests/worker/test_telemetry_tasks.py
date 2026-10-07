"""ingest_telemetry_batch (worker/telemetry_tasks.py): range index, defaults, MITRE tags, errors."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("celery")

from worker import telemetry_tasks  # noqa: E402

OS = "http://os-test:9200"


@pytest.fixture(autouse=True)
def opensearch_url(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", OS)


def _docs(body: str) -> list[tuple[dict, dict]]:
    lines = [json.loads(line) for line in body.splitlines()]
    return list(zip(lines[::2], lines[1::2], strict=True))


def test_bulk_body_targets_the_range_index_and_tags_events():
    body = telemetry_tasks.bulk_body(
        "r-1",
        [
            {"event_type": "dns_query"},
            {"event_type": "range_metrics", "@timestamp": "2026-10-07T00:00:00+00:00", "range_id": "keep"},
        ],
    )
    assert body.endswith("\n")
    (a_meta, a), (b_meta, b) = _docs(body)
    assert a_meta == b_meta == {"index": {"_index": "range-r-1"}}
    assert a["range_id"] == "r-1" and a["@timestamp"]
    assert a["mitre_technique"] == ["T1071.004"]
    assert b["range_id"] == "keep" and b["@timestamp"] == "2026-10-07T00:00:00+00:00"  # setdefault, not overwrite
    assert "mitre_technique" not in b


def test_task_posts_ndjson_to_bulk(respx_mock):
    route = respx_mock.post(f"{OS}/_bulk").mock(return_value=httpx.Response(200, json={"errors": False, "items": []}))
    result = telemetry_tasks.ingest_telemetry_batch("r-2", [{"technique_id": "T1046"}])
    assert result == {"indexed": 1, "range_id": "r-2"}
    req = route.calls.last.request
    assert req.headers["Content-Type"] == "application/x-ndjson"
    [(_, doc)] = _docs(req.content.decode())
    assert doc["mitre_technique"] == ["T1046"]


def test_partial_bulk_failure_is_logged_not_raised(respx_mock, caplog):
    items = [{"index": {"status": 201}}, {"index": {"status": 400, "error": {"type": "mapper_parsing_exception"}}}]
    respx_mock.post(f"{OS}/_bulk").mock(return_value=httpx.Response(200, json={"errors": True, "items": items}))
    with caplog.at_level("WARNING", logger="truenorth.worker"):
        result = telemetry_tasks.ingest_telemetry_batch("r-3", [{}, {}])
    assert result["indexed"] == 2
    assert "1 events failed indexing" in caplog.text


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, text="unavailable"), httpx.ConnectError("refused")],
    ids=["http-error", "unreachable"],
)
def test_opensearch_failure_raises_for_celery_to_see(respx_mock, response):
    route = respx_mock.post(f"{OS}/_bulk")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    with pytest.raises(httpx.HTTPError):
        telemetry_tasks.ingest_telemetry_batch("r-4", [{"event_type": "x"}])
