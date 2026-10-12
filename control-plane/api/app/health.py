"""TrueNorth Range - Deep health check system.

Tiers (served from app/main.py):
  - /health/live     - Liveness probe: 200 while the process serves; no dependency touched
  - /health          - Backward-compatible flags; 503 when the database is down
  - /health/ready    - Readiness probe: 503 unless DB, Redis and (when OPENSEARCH_URL is
                       set) OpenSearch answer
  - /health/deep     - Full dependency check (DB, Redis, OpenSearch, MinIO, Celery, AI,
                       Keycloak) and the signed audit export's freshness
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum

from fastapi import APIRouter

from . import audit_integrity  # noqa: F401 — registers audit_exports (deep health reads it)

logger = logging.getLogger("truenorth.health")

# The probes are unauthenticated. An exception's text names hosts, ports, users and DSN
# fragments, so it goes to the log and the response carries only this (security sweep, low).
CHECK_FAILED = "check failed; see the API log"


def _failed(exc: BaseException) -> str:
    logger.warning("health check failed: %s: %s", type(exc).__name__, exc)
    return CHECK_FAILED

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
        from .settings import app_version

        self._version = app_version()

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._start_time

    # -- Public check methods ----------------------------------------------

    async def liveness(self) -> dict:
        """Fast liveness - just confirms process is alive."""
        return {
            "status": "ok",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def readiness(self) -> SystemHealth:
        """Check critical dependencies for readiness: DB, Redis, and OpenSearch when configured."""
        from .search_backends import opensearch

        checks = [self._check_database(), self._check_redis()]
        if opensearch.configured():
            checks.append(self._check_opensearch())
        results = await asyncio.gather(*checks, return_exceptions=True)
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
            self._check_audit_export(),
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
                message=_failed(exc),
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
                message=_failed(exc),
            )

    async def _check_opensearch(self) -> ComponentHealth:
        """Verify OpenSearch cluster health."""
        start = time.monotonic()
        try:
            import httpx

            from .search_backends import connection  # URL, credentials, TLS verification

            url = connection.opensearch_url("http://localhost:9200")
            async with httpx.AsyncClient(**connection.client_kwargs(3)) as client:
                resp = await client.get(f"{url}/_cluster/health")
                resp.raise_for_status()  # 401/403: wrong credentials, not a cluster status
            elapsed = (time.monotonic() - start) * 1000
            data = resp.json()
            cluster_status = data.get("status", "red")
            status = (
                HealthStatus.HEALTHY
                if cluster_status == "green"
                else HealthStatus.DEGRADED
                if cluster_status == "yellow"
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
                message=_failed(exc),
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
                message=_failed(exc),
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
                message=_failed(exc),
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
                message=f"AI service unavailable: {_failed(exc)}",
            )

    async def _check_keycloak(self) -> ComponentHealth:
        """Verify Keycloak auth server health."""
        start = time.monotonic()
        try:
            import httpx

            url = os.getenv("KEYCLOAK_URL", "http://localhost:8180")
            realm = os.getenv("KEYCLOAK_REALM", "truenorth")
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{url}/realms/{realm}/.well-known/openid-configuration")
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
                message=_failed(exc),
            )

    async def _check_audit_export(self) -> ComponentHealth:
        """The signed daily audit export is current (ADR 0011). Reads audit_exports only.

        Degraded when audit rows older than yesterday exist but the newest export is
        older than the day before yesterday: the worker's export (beat 00:45 UTC) is not
        running, or has no AUDIT_SIGNING_SEED, or its archive refuses writes."""
        start = time.monotonic()
        try:
            from .db import SessionLocal

            db = SessionLocal()
            try:
                status, message, details = audit_export_status(db)
            finally:
                db.close()
            return ComponentHealth(name="audit_export", status=status,
                                   latency_ms=round((time.monotonic() - start) * 1000, 2),
                                   message=message, details=details)
        except Exception as exc:
            return ComponentHealth(name="audit_export", status=HealthStatus.DEGRADED,
                                   latency_ms=round((time.monotonic() - start) * 1000, 2), message=_failed(exc))

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _resolve_results(
        results: list[ComponentHealth | BaseException],
    ) -> list[ComponentHealth]:
        """Convert gathered results, replacing exceptions with UNHEALTHY."""
        resolved: list[ComponentHealth] = []
        for r in results:
            if isinstance(r, BaseException):
                resolved.append(
                    ComponentHealth(
                        name="unknown",
                        status=HealthStatus.UNHEALTHY,
                        latency_ms=0.0,
                        message=_failed(r),
                    )
                )
            else:
                resolved.append(r)
        return resolved

    def _build_system_health(self, components: list[ComponentHealth]) -> SystemHealth:
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
            timestamp=datetime.now(UTC).isoformat(),
        )


def audit_export_status(db) -> tuple[HealthStatus, str, dict]:
    """(status, message, details) of the audit export, from audit_exports and audit_logs."""
    from datetime import timedelta

    from sqlalchemy import func

    from .audit_integrity import AuditExport
    from .models import AuditLog

    if db is None:
        return HealthStatus.HEALTHY, "", {}
    today = datetime.now(UTC).date()
    latest = db.query(func.max(AuditExport.day)).scalar()
    oldest = db.query(func.min(AuditLog.timestamp)).scalar()
    details = {"latest_export_day": latest.isoformat() if latest else None}
    if latest is not None and latest >= today - timedelta(days=2):
        return HealthStatus.HEALTHY, f"exported through {latest}", details
    if oldest is None:
        return HealthStatus.HEALTHY, "no audit rows yet", details
    oldest_day = (oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)).astimezone(UTC).date()
    if latest is None and oldest_day >= today - timedelta(days=1):
        return HealthStatus.HEALTHY, "no finished day to export yet", details
    return (HealthStatus.DEGRADED,
            f"audit export behind: latest {latest or 'none'}; check the worker's export (AUDIT_SIGNING_SEED, archive)",
            details)


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
