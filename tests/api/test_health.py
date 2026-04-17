"""Tests for control-plane/api/app/health.py - Health check system."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the control-plane package is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "control-plane", "api"))

from app.health import (
    ComponentHealth,
    HealthChecker,
    HealthStatus,
    SystemHealth,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def checker() -> HealthChecker:
    """Fresh HealthChecker instance for each test."""
    return HealthChecker()


# ---------------------------------------------------------------------------
# test_liveness_always_ok
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_always_ok(checker: HealthChecker):
    """Liveness probe must always return ``status: ok``."""
    result = await checker.liveness()
    assert result["status"] == "ok"
    assert "timestamp" in result


# ---------------------------------------------------------------------------
# test_readiness_returns_components
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_returns_components(checker: HealthChecker):
    """Readiness probe returns SystemHealth with database + redis components."""
    with (
        patch.object(checker, "_check_database", new_callable=AsyncMock) as mock_db,
        patch.object(checker, "_check_redis", new_callable=AsyncMock) as mock_redis,
    ):
        mock_db.return_value = ComponentHealth(
            name="database",
            status=HealthStatus.HEALTHY,
            latency_ms=1.5,
            message="OK",
        )
        mock_redis.return_value = ComponentHealth(
            name="redis",
            status=HealthStatus.HEALTHY,
            latency_ms=0.8,
            message="PONG",
        )

        result = await checker.readiness()

    assert isinstance(result, SystemHealth)
    assert result.status == HealthStatus.HEALTHY
    component_names = {c.name for c in result.components}
    assert "database" in component_names
    assert "redis" in component_names


# ---------------------------------------------------------------------------
# test_deep_check_returns_all_components
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_check_returns_all_components(checker: HealthChecker):
    """Deep check must report all seven infrastructure components."""
    expected_names = {
        "database",
        "redis",
        "opensearch",
        "minio",
        "celery",
        "ai_orchestrator",
        "keycloak",
    }

    async def _healthy_stub(name: str) -> ComponentHealth:
        return ComponentHealth(
            name=name,
            status=HealthStatus.HEALTHY,
            latency_ms=1.0,
            message="OK",
        )

    with (
        patch.object(checker, "_check_database", new=lambda: _healthy_stub("database")),
        patch.object(checker, "_check_redis", new=lambda: _healthy_stub("redis")),
        patch.object(checker, "_check_opensearch", new=lambda: _healthy_stub("opensearch")),
        patch.object(checker, "_check_minio", new=lambda: _healthy_stub("minio")),
        patch.object(checker, "_check_celery", new=lambda: _healthy_stub("celery")),
        patch.object(checker, "_check_ai_orchestrator", new=lambda: _healthy_stub("ai_orchestrator")),
        patch.object(checker, "_check_keycloak", new=lambda: _healthy_stub("keycloak")),
    ):
        result = await checker.deep_check()

    assert isinstance(result, SystemHealth)
    returned_names = {c.name for c in result.components}
    assert returned_names == expected_names
    assert result.status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# test_health_response_format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_response_format(checker: HealthChecker):
    """SystemHealth serializes to the expected JSON shape."""
    with (
        patch.object(checker, "_check_database", new_callable=AsyncMock) as mock_db,
        patch.object(checker, "_check_redis", new_callable=AsyncMock) as mock_redis,
    ):
        mock_db.return_value = ComponentHealth(
            name="database",
            status=HealthStatus.HEALTHY,
            latency_ms=2.0,
            message="OK",
        )
        mock_redis.return_value = ComponentHealth(
            name="redis",
            status=HealthStatus.DEGRADED,
            latency_ms=5.0,
            message="slow",
        )
        result = await checker.readiness()

    data = asdict(result)
    # Top-level keys
    assert set(data.keys()) == {"status", "version", "uptime_seconds", "components", "timestamp"}
    # Components are dicts
    assert isinstance(data["components"], list)
    assert all(isinstance(c, dict) for c in data["components"])
    # Aggregate should be DEGRADED because redis is degraded
    assert data["status"] == HealthStatus.DEGRADED.value


# ---------------------------------------------------------------------------
# test_component_health_dataclass
# ---------------------------------------------------------------------------


def test_component_health_dataclass():
    """ComponentHealth dataclass serialization works correctly."""
    comp = ComponentHealth(
        name="test-service",
        status=HealthStatus.UNHEALTHY,
        latency_ms=42.5,
        message="Connection refused",
        details={"host": "localhost", "port": 5432},
    )
    data = asdict(comp)
    assert data["name"] == "test-service"
    assert data["status"] == "unhealthy"
    assert data["latency_ms"] == 42.5
    assert data["message"] == "Connection refused"
    assert data["details"]["host"] == "localhost"
