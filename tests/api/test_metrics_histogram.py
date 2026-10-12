"""The hand-rolled Prometheus histogram in app/metrics.py exports valid buckets.

``observe`` used to bump every bucket the value fitted (already cumulative) and
``collect`` summed them again, so one 15 ms request exported le=0.025 -> 1, le=0.05 -> 2,
... le=10 -> 9 and +Inf -> 1. ``histogram_quantile`` over that non-monotonic series read
a constant 7.75 s on staging and ApiHighLatencyP95 fired while real latency was ~15 ms.
"""

from __future__ import annotations

import re

import pytest
from app.metrics import Histogram, PrometheusMiddleware, http_requests_in_flight
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _buckets(text: str, name: str = "h") -> dict[str, int]:
    return {m[1]: int(m[2]) for m in re.finditer(rf'^{name}_bucket\{{.*?le="([^"]+)"\}} (\d+)$', text, re.M)}


def _count(text: str, name: str = "h") -> int:
    m = re.search(rf"^{name}_count(?:\{{.*?\}})? (\d+)$", text, re.M)
    assert m, text
    return int(m[1])


def test_one_15ms_observation_lands_in_the_right_buckets():
    h = Histogram("h", "test")
    h.observe(value=0.015)
    b = _buckets(h.collect())
    assert b["0.005"] == 0
    assert b["0.01"] == 0
    for le in ("0.025", "0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "+Inf"):
        assert b[le] == 1, (le, b)


@pytest.mark.parametrize(
    "values",
    [
        [0.015],
        [0.001, 0.015, 0.2, 3.0, 99.0],
        [0.005, 0.01, 10.0],  # exactly on a bound counts in that bucket (le is <=)
        [50.0, 60.0],  # above every finite bound: only +Inf
    ],
)
def test_buckets_are_monotonic_and_inf_equals_count(values):
    h = Histogram("h", "test", ("method", "path"))
    for v in values:
        h.observe("GET", "/x", value=v)
    text = h.collect()
    b = _buckets(text)
    series = [b[f"{le:g}"] for le in Histogram.DEFAULT_BUCKETS] + [b["+Inf"]]
    assert series == sorted(series), series
    assert b["+Inf"] == _count(text) == len(values)
    for le in Histogram.DEFAULT_BUCKETS:
        assert b[f"{le:g}"] == sum(1 for v in values if v <= le), (le, b)


def _app_that_raises() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    return app


def test_in_flight_returns_to_its_level_after_an_exception():
    before = http_requests_in_flight._values[()]
    client = TestClient(_app_that_raises(), raise_server_exceptions=False)
    for _ in range(3):
        assert client.get("/boom").status_code == 500
    after = http_requests_in_flight._values[()]
    assert after == before, (before, after)
    assert after >= 0


@pytest.mark.parametrize("path", ["/health", "/health/live", "/health/ready", "/health/deep"])
def test_health_probes_are_not_timed(client, path):
    client.get(path)  # 200 or 503 depending on the test env's Redis; either way, not timed
    body = client.get("/metrics").text
    durations = [ln for ln in body.splitlines() if ln.startswith("http_request_duration_seconds")]
    assert not any('path="/health' in ln for ln in durations), [ln for ln in durations if "/health" in ln]
    assert f'path="{path}"' not in body


def test_exported_api_histogram_is_valid(client):
    client.get("/ranges")
    body = client.get("/metrics").text
    per_series: dict[str, dict[str, int]] = {}
    for m in re.finditer(r'^http_request_duration_seconds_bucket\{(.*),le="([^"]+)"\} (\d+)$', body, re.M):
        per_series.setdefault(m[1], {})[m[2]] = int(m[3])
    assert per_series, body
    for labels, b in per_series.items():
        series = [b[f"{le:g}"] for le in Histogram.DEFAULT_BUCKETS] + [b["+Inf"]]
        assert series == sorted(series), (labels, series)
        count = re.search(rf"^http_request_duration_seconds_count\{{{re.escape(labels)}\}} (\d+)$", body, re.M)
        assert count and int(count[1]) == b["+Inf"], labels
