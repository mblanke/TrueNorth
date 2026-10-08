"""Health endpoints tell the truth (app/main.py, app/health.py).

Before: /health answered 200 ``status: ok`` with the database down, and /health/ready
answered 200 whatever it found, so no probe could ever take a broken API out of
rotation. Now /health/live touches nothing, /health is 503 when the database is down,
and /health/ready is 503 when the database, Redis or (if configured) OpenSearch is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from app import main as api_main
from app.db import get_db
from app.health import ComponentHealth, HealthStatus


def _component(name: str, status: HealthStatus) -> ComponentHealth:
    return ComponentHealth(name=name, status=status, latency_ms=0.1)


def test_liveness_needs_no_dependency(client, monkeypatch):
    monkeypatch.setattr(api_main._checker, "_check_database", AsyncMock(side_effect=AssertionError("touched")))
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_health_is_200_with_a_working_database(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] is True and r.json()["status"] == "ok"


def test_health_is_503_when_the_database_is_down(client):
    class _DeadSession:
        def execute(self, *_a, **_k):
            raise RuntimeError("connection refused")

    def _dead_db():
        yield _DeadSession()

    previous = api_main.app.dependency_overrides.get(get_db)
    api_main.app.dependency_overrides[get_db] = _dead_db
    try:
        r = client.get("/health")
    finally:
        api_main.app.dependency_overrides[get_db] = previous
    assert r.status_code == 503
    body = r.json()
    assert body["db"] is False and body["status"] == "unavailable"


def test_health_reports_the_running_release(client, monkeypatch):
    monkeypatch.setattr(api_main, "APP_VERSION", "3.1.4")
    assert client.get("/health").json()["version"] == "3.1.4"


@pytest.mark.parametrize(
    ("db", "redis", "code"),
    [
        (HealthStatus.HEALTHY, HealthStatus.HEALTHY, 200),
        (HealthStatus.UNHEALTHY, HealthStatus.HEALTHY, 503),
        (HealthStatus.HEALTHY, HealthStatus.UNHEALTHY, 503),
    ],
)
def test_readiness_is_503_when_a_critical_dependency_is_down(client, monkeypatch, db, redis, code):
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    monkeypatch.setattr(api_main._checker, "_check_database", AsyncMock(return_value=_component("database", db)))
    monkeypatch.setattr(api_main._checker, "_check_redis", AsyncMock(return_value=_component("redis", redis)))
    r = client.get("/health/ready")
    assert r.status_code == code, r.text


def test_readiness_checks_opensearch_only_when_configured(client, monkeypatch):
    ok = HealthStatus.HEALTHY
    monkeypatch.setattr(api_main._checker, "_check_database", AsyncMock(return_value=_component("database", ok)))
    monkeypatch.setattr(api_main._checker, "_check_redis", AsyncMock(return_value=_component("redis", ok)))
    down = AsyncMock(return_value=_component("opensearch", HealthStatus.UNHEALTHY))
    monkeypatch.setattr(api_main._checker, "_check_opensearch", down)

    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    assert client.get("/health/ready").status_code == 200
    down.assert_not_called()

    monkeypatch.setenv("OPENSEARCH_URL", "http://opensearch:9200")
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert "opensearch" in {c["name"] for c in r.json()["components"]}


def test_readiness_tolerates_a_yellow_cluster(client, monkeypatch):
    ok = HealthStatus.HEALTHY
    monkeypatch.setenv("OPENSEARCH_URL", "http://opensearch:9200")
    monkeypatch.setattr(api_main._checker, "_check_database", AsyncMock(return_value=_component("database", ok)))
    monkeypatch.setattr(api_main._checker, "_check_redis", AsyncMock(return_value=_component("redis", ok)))
    yellow = AsyncMock(return_value=_component("opensearch", HealthStatus.DEGRADED))
    monkeypatch.setattr(api_main._checker, "_check_opensearch", yellow)
    assert client.get("/health/ready").status_code == 200
