"""TrueNorth Range - Deep health check system.

Three tiers:
  - /health          - Fast liveness probe (always 200 unless process dying)
  - /health/ready    - Readiness probe (checks DB + Redis connectivity)
  - /health/deep     - Full dependency check (DB, Redis, OpenSearch, MinIO, Celery, AI)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter


# -- Status Enum -----------------------------------------------------------

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# -- Data Classes ----------------------------------------------------------

@dataclass
class ComponentHealth:
    """Health of a single infrastructure component."""
    name: str
    status: HealthStatus
    latency_ms: float
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class SystemHealth:
    """Aggregate system health report."""
    status: HealthStatus
    version: str
    uptime_seconds: float
    components: list[ComponentHealth]
    timestamp: str


# -- Health Checker --------------------------------------------------------

class HealthChecker:
    """Checks health of all TrueNorth Range dependencies."""

    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._version = os.getenv("APP_VERSION", "0.1.0")

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._start_time

    # -- Public check methods ----------------------------------------------

    async def liveness(self) -> dict:
        """Fast liveness - just confirms process is alive."""
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def readiness(self) -> SystemHealth:
        """Check critical dependencies for readiness (DB + Redis)."""
        results = await asyncio.gather(
            self._check_database(),
            self._check_redis(),
            return_exceptions=True,
        )
        components = self._resolve_results(results)
        return self._build_system_health(components)

    async def deep_check(self) -> SystemHealth:
        """Full dependency audit - every backing service."""
        results = await asyncio.gather(
            self._check_database(),
            self._check_redis(),
            self._check_opensearch(),
            self._check_minio(),
            self._check_celery(),
            self._check_ai_orchestrator(),
            self._check_keycloak(),
            return_exceptions=True,
        )
        components = self._resolve_results(results)
        return self._build_system_health(components)

    # -- Individual component checks ---------------------------------------

    async def _check_database(self) -> ComponentHealth:
        """Verify PostgreSQL / PgBouncer connectivity."""
        start = time.monotonic()
        try:
            from .db import check_db_health  # noqa: F811
            healthy = check_db_health()
            elapsed = (time.monotonic() - start) * 1000
            if healthy:
                return ComponentHealth(
                    name="database",
                    status=HealthStatus.HEALTHY,
                    latency_ms=round(elapsed, 2),
                    message="PostgreSQL connection OK",
                )
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message="Database health check returned False",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    async def _check_redis(self) -> ComponentHealth:
        """Verify Redis connectivity via PING."""
        start = time.monotonic()
        try:
            import redis as redis_lib
            url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            r = redis_lib.from_url(url, socket_connect_timeout=2)
            pong = r.ping()
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="redis",
                status=HealthStatus.HEALTHY if pong else HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message="PONG" if pong else "No PONG",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    async def _check_opensearch(self) -> ComponentHealth:
        """Verify OpenSearch cluster health."""
        start = time.monotonic()
        try:
            import httpx
            url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{url}/_cluster/health")
            elapsed = (time.monotonic() - start) * 1000
            data = resp.json()
            cluster_status = data.get("status", "red")
            status = (
                HealthStatus.HEALTHY if cluster_status == "green"
                else HealthStatus.DEGRADED if cluster_status == "yellow"
                else HealthStatus.UNHEALTHY
            )
            return ComponentHealth(
                name="opensearch",
                status=status,
                latency_ms=round(elapsed, 2),
                message=f"Cluster status: {cluster_status}",
                details={"cluster_name": data.get("cluster_name", "")},
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="opensearch",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    async def _check_minio(self) -> ComponentHealth:
        """Verify MinIO S3 connectivity."""
        start = time.monotonic()
        try:
            import httpx
            endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
            if not endpoint.startswith("http"):
                endpoint = f"http://{endpoint}"
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{endpoint}/minio/health/live")
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="minio",
                status=HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="minio",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    async def _check_celery(self) -> ComponentHealth:
        """Verify Celery broker connectivity via Redis."""
        start = time.monotonic()
        try:
            import redis as redis_lib
            url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            r = redis_lib.from_url(url, socket_connect_timeout=2)
            # Check if any Celery-related keys exist (best-effort)
            info = r.info("clients")
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="celery",
                status=HealthStatus.HEALTHY,
                latency_ms=round(elapsed, 2),
                message="Broker reachable",
                details={"connected_clients": info.get("connected_clients", 0)},
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="celery",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    async def _check_ai_orchestrator(self) -> ComponentHealth:
        """Verify AI orchestrator service health."""
        start = time.monotonic()
        try:
            import httpx
            base = os.getenv("AI_ORCHESTRATOR_URL", "http://localhost:6000")
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{base}/health")
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="ai_orchestrator",
                status=HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.DEGRADED,
                latency_ms=round(elapsed, 2),
                message=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="ai_orchestrator",
                status=HealthStatus.DEGRADED,
                latency_ms=round(elapsed, 2),
                message=f"AI service unavailable: {exc}",
            )

    async def _check_keycloak(self) -> ComponentHealth:
        """Verify Keycloak auth server health."""
        start = time.monotonic()
        try:
            import httpx
            url = os.getenv("KEYCLOAK_URL", "http://localhost:8180")
            realm = os.getenv("KEYCLOAK_REALM", "truenorth")
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(
                    f"{url}/realms/{realm}/.well-known/openid-configuration"
                )
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="keycloak",
                status=HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="keycloak",
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed, 2),
                message=str(exc),
            )

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _resolve_results(
        results: list[ComponentHealth | BaseException],
    ) -> list[ComponentHealth]:
        """Convert gathered results, replacing exceptions with UNHEALTHY."""
        resolved: list[ComponentHealth] = []
        for r in results:
            if isinstance(r, BaseException):
                resolved.append(ComponentHealth(
                    name="unknown",
                    status=HealthStatus.UNHEALTHY,
                    latency_ms=0.0,
                    message=str(r),
                ))
            else:
                resolved.append(r)
        return resolved

    def _build_system_health(
        self, components: list[ComponentHealth]
    ) -> SystemHealth:
        """Determine aggregate status from component checks."""
        statuses = {c.status for c in components}
        if HealthStatus.UNHEALTHY in statuses:
            aggregate = HealthStatus.UNHEALTHY
        elif HealthStatus.DEGRADED in statuses:
            aggregate = HealthStatus.DEGRADED
        else:
            aggregate = HealthStatus.HEALTHY

        return SystemHealth(
            status=aggregate,
            version=self._version,
            uptime_seconds=round(self.uptime, 2),
            components=components,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


# -- FastAPI Router --------------------------------------------------------

health_router = APIRouter(tags=["health"])
_checker = HealthChecker()


@health_router.get("/health", summary="Liveness probe")
async def liveness():
    """Fast liveness probe - always 200 unless the process is dying."""
    return await _checker.liveness()


@health_router.get("/health/ready", summary="Readiness probe")
async def readiness():
    """Readiness probe - checks DB + Redis connectivity."""
    result = await _checker.readiness()
    return asdict(result)


@health_router.get("/health/deep", summary="Deep health check")
async def deep_health():
    """Full dependency audit of all backing services."""
    result = await _checker.deep_check()
    return asdict(result)