"""
TrueNorth Range — Prometheus Metrics Middleware for FastAPI

Drop-in middleware that exposes standard HTTP, range-provisioning,
and Celery task metrics at /metrics.

Usage:
    from monitoring.fastapi_metrics import setup_metrics
    app = FastAPI()
    setup_metrics(app)
"""

from __future__ import annotations

import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

# ── Registry ────────────────────────────────────────────────────
# Use the default registry so all metrics are auto-collected.
# For multiprocess deployments (gunicorn pre-fork) swap to a
# multiprocess-aware registry.

# ── HTTP Metrics ────────────────────────────────────────────────
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "path", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=["method", "path", "status"],
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed",
)

# ── Range Provisioning Metrics ──────────────────────────────────
RANGE_PROVISIONS_TOTAL = Counter(
    "range_provisions_total",
    "Total range provision attempts",
    labelnames=["status"],  # success | failed
)

RANGE_PROVISION_DURATION = Histogram(
    "range_provision_duration_seconds",
    "Duration of range provisioning operations in seconds",
    buckets=(5, 15, 30, 60, 120, 180, 300, 600, 900, 1800),
)

# ── Celery Task Metrics ────────────────────────────────────────
CELERY_TASKS_TOTAL = Counter(
    "celery_tasks_total",
    "Total Celery tasks executed",
    labelnames=["task_name", "status"],  # success | failed | retry
)

# ── Business Gauges ────────────────────────────────────────────
ACTIVE_RANGES = Gauge(
    "active_ranges",
    "Number of active cyber ranges by state",
    labelnames=["state"],  # draft | provisioning | ready | failed | destroying
)

ACTIVE_EXERCISES = Gauge(
    "active_exercises",
    "Number of active exercises currently running",
)


def _normalize_path(request: Request) -> str:
    """
    Collapse path parameters into placeholders so Prometheus labels
    stay low-cardinality.  E.g. /api/v1/ranges/abc123 -> /api/v1/ranges/{id}
    """
    route = request.scope.get("route")
    if route and isinstance(route, APIRoute):
        return route.path  # Use the FastAPI route template
    return request.url.path


async def _metrics_middleware(request: Request, call_next: Callable) -> Response:
    """ASGI middleware that records request duration and counts."""
    if request.url.path == "/metrics":
        return await call_next(request)

    method = request.method
    path = _normalize_path(request)

    HTTP_REQUESTS_IN_PROGRESS.inc()
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        # Record 500 for unhandled exceptions, then re-raise
        duration = time.perf_counter() - start
        HTTP_REQUEST_DURATION.labels(method=method, path=path, status="500").observe(duration)
        HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status="500").inc()
        raise
    finally:
        HTTP_REQUESTS_IN_PROGRESS.dec()

    duration = time.perf_counter() - start
    status = str(response.status_code)

    HTTP_REQUEST_DURATION.labels(method=method, path=path, status=status).observe(duration)
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status).inc()

    return response


async def _metrics_endpoint(_request: Request) -> Response:
    """Serve Prometheus metrics at /metrics."""
    body = generate_latest()
    return PlainTextResponse(content=body, media_type=CONTENT_TYPE_LATEST)


def setup_metrics(app: FastAPI) -> None:
    """
    Attach Prometheus metrics middleware and /metrics endpoint to a
    FastAPI application.

    Call once during app startup:

        app = FastAPI()
        setup_metrics(app)
    """
    app.middleware("http")(_metrics_middleware)
    app.add_route("/metrics", _metrics_endpoint, methods=["GET"])


# ── Convenience helpers for application code ───────────────────

def record_provision(status: str, duration_seconds: float) -> None:
    """
    Record a range-provision outcome.

    Args:
        status: "success" or "failed"
        duration_seconds: wall-clock seconds the provision took
    """
    RANGE_PROVISIONS_TOTAL.labels(status=status).inc()
    RANGE_PROVISION_DURATION.observe(duration_seconds)


def record_celery_task(task_name: str, status: str) -> None:
    """
    Record a Celery task completion.

    Args:
        task_name: e.g. "provision_range", "run_scenario"
        status: "success", "failed", or "retry"
    """
    CELERY_TASKS_TOTAL.labels(task_name=task_name, status=status).inc()


def set_active_ranges(state: str, count: int) -> None:
    """Set the gauge for active ranges in a given state."""
    ACTIVE_RANGES.labels(state=state).set(count)


def set_active_exercises(count: int) -> None:
    """Set the gauge for active exercises."""
    ACTIVE_EXERCISES.set(count)