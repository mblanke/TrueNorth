"""
Prometheus-compatible metrics for the TrueNorth Range FastAPI application.

Self-contained — no external prometheus_client dependency required.
Generates Prometheus text exposition format (v0.0.4) directly.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

# ── Registry ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()


class Counter:
    """Prometheus-style counter (monotonically increasing)."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = defaultdict(float)

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        with _lock:
            self._values[label_values] += amount

    def collect(self) -> str:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        with _lock:
            for labels, value in sorted(self._values.items()):
                lbl = self._format_labels(labels)
                lines.append(f"{self.name}{lbl} {self._fmt(value)}")
        return "\n".join(lines)

    # ── helpers ──────────────────────────────────────────────────────────
    def _format_labels(self, values: tuple[str, ...]) -> str:
        if not self.label_names:
            return ""
        pairs = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, values, strict=False))
        return "{" + pairs + "}"

    @staticmethod
    def _fmt(v: float) -> str:
        return str(int(v)) if v == int(v) else f"{v:.6g}"


class Gauge:
    """Prometheus-style gauge (can go up or down)."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = defaultdict(float)

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        with _lock:
            self._values[label_values] += amount

    def dec(self, *label_values: str, amount: float = 1.0) -> None:
        with _lock:
            self._values[label_values] -= amount

    def set(self, *label_values: str, value: float) -> None:
        with _lock:
            self._values[label_values] = value

    def collect(self) -> str:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
        ]
        with _lock:
            for labels, value in sorted(self._values.items()):
                lbl = self._format_labels(labels)
                lines.append(f"{self.name}{lbl} {Counter._fmt(value)}")
        return "\n".join(lines)

    def _format_labels(self, values: tuple[str, ...]) -> str:
        if not self.label_names:
            return ""
        pairs = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, values, strict=False))
        return "{" + pairs + "}"


class Histogram:
    """Prometheus-style histogram with configurable buckets."""

    DEFAULT_BUCKETS: tuple[float, ...] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.buckets = buckets or self.DEFAULT_BUCKETS
        # keyed by label_values → {bucket_bound: count}
        self._counts: dict[tuple[str, ...], dict[float, int]] = defaultdict(lambda: defaultdict(int))
        self._sums: dict[tuple[str, ...], float] = defaultdict(float)
        self._totals: dict[tuple[str, ...], int] = defaultdict(int)

    def observe(self, *label_values: str, value: float) -> None:
        with _lock:
            self._sums[label_values] += value
            self._totals[label_values] += 1
            for b in self.buckets:
                if value <= b:
                    self._counts[label_values][b] += 1

    def collect(self) -> str:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        fmt = Counter._fmt
        with _lock:
            all_keys = sorted(set(self._totals.keys()) | set(self._sums.keys()) | set(self._counts.keys()))
            for labels in all_keys:
                lbl_base = self._format_labels(labels)
                cumulative = 0
                for b in self.buckets:
                    cumulative += self._counts[labels].get(b, 0)
                    le_lbl = self._add_label(lbl_base, "le", fmt(b))
                    lines.append(f"{self.name}_bucket{le_lbl} {cumulative}")
                inf_lbl = self._add_label(lbl_base, "le", "+Inf")
                lines.append(f"{self.name}_bucket{inf_lbl} {self._totals[labels]}")
                lines.append(f"{self.name}_sum{lbl_base} {fmt(self._sums[labels])}")
                lines.append(f"{self.name}_count{lbl_base} {self._totals[labels]}")
        return "\n".join(lines)

    # ── helpers ──────────────────────────────────────────────────────────
    def _format_labels(self, values: tuple[str, ...]) -> str:
        if not self.label_names:
            return ""
        pairs = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, values, strict=False))
        return "{" + pairs + "}"

    @staticmethod
    def _add_label(base: str, key: str, val: str) -> str:
        new = f'{key}="{val}"'
        if base:
            return base[:-1] + "," + new + "}"
        return "{" + new + "}"


# ── Metric instances ─────────────────────────────────────────────────────────

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ("method", "path"),
)

http_requests_in_flight = Gauge(
    "http_requests_in_flight",
    "Number of HTTP requests currently being processed",
)

active_websocket_connections = Gauge(
    "active_websocket_connections",
    "Number of active WebSocket connections",
)

range_provisioning_total = Counter(
    "range_provisioning_total",
    "Total range provisioning operations",
    ("status",),
)

celery_tasks_total = Counter(
    "celery_tasks_total",
    "Total Celery tasks executed",
    ("task", "status"),
)

# ── Metrics router ───────────────────────────────────────────────────────────

_ALL_METRICS: list[Any] = [
    http_requests_total,
    http_request_duration_seconds,
    http_requests_in_flight,
    active_websocket_connections,
    range_provisioning_total,
    celery_tasks_total,
]

router = APIRouter(tags=["monitoring"])


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Expose metrics in Prometheus text exposition format."""
    body = "\n\n".join(m.collect() for m in _ALL_METRICS) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ── Middleware ────────────────────────────────────────────────────────────────


def _normalize_path(path: str) -> str:
    """Collapse dynamic path segments to reduce cardinality.

    Examples:
        /api/v1/ranges/abc-123          → /api/v1/ranges/{id}
        /api/v1/ranges/abc-123/nodes/5  → /api/v1/ranges/{id}/nodes/{id}
    """
    import re

    # UUID-like or numeric ids
    path = re.sub(
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "/{id}",
        path,
    )
    path = re.sub(r"/\d+", "/{id}", path)
    return path


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Lightweight ASGI middleware that records HTTP metrics."""

    # Paths to skip recording (avoid cardinality explosion and self-measurement)
    SKIP_PATHS = frozenset({"/metrics", "/health", "/healthz", "/ready"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        method = request.method
        path = _normalize_path(request.url.path)

        http_requests_in_flight.inc()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            http_requests_total.inc(method, path, "500")
            http_requests_in_flight.dec()
            raise
        else:
            duration = time.perf_counter() - start
            status = str(response.status_code)
            http_requests_total.inc(method, path, status)
            http_request_duration_seconds.observe(method, path, value=duration)
            return response
        finally:
            http_requests_in_flight.dec()
