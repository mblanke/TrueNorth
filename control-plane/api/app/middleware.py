"""
TrueNorth Range — Security Hardening Middleware
================================================
Comprehensive FastAPI middleware providing:
  - Sliding-window rate limiting (Redis ZSET)
  - Security headers
  - Request-ID propagation
  - Structured JSON request logging
  - Input sanitisation
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# Context variables (available throughout the request lifecycle)
# ---------------------------------------------------------------------------
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
tenant_id_ctx: ContextVar[str] = ContextVar("tenant_id", default="")
user_id_ctx: ContextVar[str] = ContextVar("user_id", default="")

# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("truenorth.middleware")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# =========================================================================
# 1. RATE LIMITING (Redis sliding-window via ZSET)
# =========================================================================

# Per-route overrides: (method, path_prefix) -> requests/min
_DEFAULT_ROUTE_LIMITS: Dict[Tuple[str, str], int] = {
    ("POST", "/ranges/batch-provision"): 5,
    ("POST", "/ranges"): 30,
    ("GET", ""): 200,          # all GET endpoints
}

_UNLIMITED_PATHS: List[str] = ["/health"]


def _match_route_limit(method: str, path: str, default: int) -> int:
    """Return the rate-limit (req/min) for a given method+path."""
    for (m, prefix), limit in _DEFAULT_ROUTE_LIMITS.items():
        if method == m and (prefix == "" or path.startswith(prefix)):
            return limit
    return default


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter backed by Redis ZSET."""

    def __init__(
        self,
        app: ASGIApp,
        redis_client: Any = None,
        enabled: bool = True,
        default_limit: int = 100,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self.redis = redis_client
        self.enabled = enabled
        self.default_limit = default_limit
        self.window = window_seconds

    # ----- helpers ----------------------------------------------------------

    @staticmethod
    def _client_key(request: Request) -> str:
        """Key by tenant_id (if authenticated) else client IP."""
        tenant = getattr(request.state, "tenant_id", None)
        if tenant:
            return f"rl:tenant:{tenant}"
        forwarded = request.headers.get("x-forwarded-for")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )
        return f"rl:ip:{ip}"

    async def _check_rate_limit(
        self, key: str, limit: int, now: float
    ) -> Tuple[bool, int, int]:
        """
        Returns (allowed, remaining, reset_epoch).
        Uses a sorted-set with score = request timestamp.
        """
        window_start = now - self.window
        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, self.window * 2)
        results = await pipe.execute()  # type: ignore[union-attr]

        current_count: int = results[2]
        remaining = max(0, limit - current_count)
        reset_at = int(now) + self.window

        if current_count > limit:
            # Remove the entry we just optimistically added
            await self.redis.zrem(key, str(now))
            return False, 0, reset_at

        return True, remaining, reset_at

    # ----- dispatch ---------------------------------------------------------

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self.enabled or self.redis is None:
            return await call_next(request)

        path = request.url.path
        method = request.method

        # Unlimited endpoints
        if any(path.startswith(p) for p in _UNLIMITED_PATHS):
            return await call_next(request)

        limit = _match_route_limit(method, path, self.default_limit)
        key = self._client_key(request)
        now = time.time()

        try:
            allowed, remaining, reset_at = await self._check_rate_limit(
                key, limit, now
            )
        except Exception:
            # Redis down → degrade gracefully, allow the request
            logger.warning("Rate-limiter Redis error; allowing request", exc_info=True)
            return await call_next(request)

        if not allowed:
            retry_after = self.window
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded. Try again later.",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_at)
        return response


# =========================================================================
# 2. SECURITY HEADERS
# =========================================================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject hardening headers on every response."""

    HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Content-Security-Policy": "default-src 'self'",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        for header, value in self.HEADERS.items():
            response.headers[header] = value

        # HSTS only when the connection is (or was) over TLS
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        # Strip the Server header if present
        for _server_key in ("Server", "server"):
            if _server_key in response.headers:
                del response.headers[_server_key]

        return response


# =========================================================================
# 3. REQUEST ID
# =========================================================================

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Ensure every request has a unique X-Request-ID.
    Accepts a valid client-provided UUID; otherwise generates one.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        if incoming and _UUID_RE.match(incoming):
            rid = incoming
        else:
            rid = str(uuid.uuid4())

        # Store in context var for downstream consumers
        request_id_ctx.set(rid)
        request.state.request_id = rid

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


# =========================================================================
# 4. REQUEST LOGGING
# =========================================================================

_QUIET_PATHS: List[str] = ["/health", "/metrics"]
_SLOW_THRESHOLD_MS: float = 1000.0


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured JSON logging of every request."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        verbose = not any(path.startswith(qp) for qp in _QUIET_PATHS)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        rid = request_id_ctx.get("")
        tid = tenant_id_ctx.get("")
        uid = user_id_ctx.get("")

        log_entry: Dict[str, Any] = {
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "request_id": rid,
            "tenant_id": tid,
            "user_id": uid,
        }

        if verbose:
            if duration_ms >= _SLOW_THRESHOLD_MS:
                logger.warning(json.dumps(log_entry))
            else:
                logger.info(json.dumps(log_entry))

        return response


# =========================================================================
# 5. INPUT SANITISATION
# =========================================================================

_MAX_REQUEST_SIZE_DEFAULT = 10 * 1024 * 1024  # 10 MB

_METHODS_REQUIRING_CONTENT_TYPE = {"POST", "PUT", "PATCH"}
_ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
}


class InputSanitizationMiddleware(BaseHTTPMiddleware):
    """Guard against oversized, malformed, or null-byte payloads."""

    def __init__(
        self,
        app: ASGIApp,
        max_request_size: int = _MAX_REQUEST_SIZE_DEFAULT,
    ) -> None:
        super().__init__(app)
        self.max_request_size = max_request_size

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        method = request.method

        # --- Payload size guard -------------------------------------------
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_request_size:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Payload too large. Max allowed: "
                        f"{self.max_request_size} bytes."
                    )
                },
            )

        # --- Content-Type validation on mutating methods ------------------
        if method in _METHODS_REQUIRING_CONTENT_TYPE:
            ct = request.headers.get("content-type", "")
            base_ct = ct.split(";")[0].strip().lower()
            if ct and base_ct not in _ALLOWED_CONTENT_TYPES:
                return JSONResponse(
                    status_code=415,
                    content={
                        "detail": (
                            f"Unsupported Content-Type: {base_ct}. "
                            f"Allowed: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}"
                        )
                    },
                )

        # --- Null-byte stripping (via receive wrapper) --------------------
        original_receive = request._receive  # type: ignore[attr-defined]

        async def _sanitised_receive():
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes) and b"\x00" in body:
                    message["body"] = body.replace(b"\x00", b"")
            return message

        request._receive = _sanitised_receive  # type: ignore[attr-defined]

        return await call_next(request)


# =========================================================================
# 6. SETUP HELPER
# =========================================================================

def setup_middleware(
    app: FastAPI,
    redis_url: Optional[str] = None,
) -> None:
    """
    One-call middleware setup.

    Call this **after** mounting routes so that middleware wraps all of them.

    Parameters
    ----------
    app : FastAPI
        The application instance.
    redis_url : str | None
        Redis connection URL for rate limiting.  When *None* and
        ``RATE_LIMIT_ENABLED`` is not ``"true"``, rate limiting is skipped.

    Environment variables
    ---------------------
    RATE_LIMIT_ENABLED : str
        ``"true"`` to enable rate limiting (default ``"true"``).
    RATE_LIMIT_DEFAULT : str
        Default requests-per-minute (default ``"100"``).
    MAX_REQUEST_SIZE : str
        Maximum request body in bytes (default ``"10485760"`` = 10 MB).
    REDIS_URL : str
        Fallback redis URL if *redis_url* parameter is not provided.
    """

    # --- Resolve configuration from env -----------------------------------
    rate_limit_enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    rate_limit_default = int(os.getenv("RATE_LIMIT_DEFAULT", "100"))
    max_request_size = int(os.getenv("MAX_REQUEST_SIZE", str(_MAX_REQUEST_SIZE_DEFAULT)))
    effective_redis_url = redis_url or os.getenv("REDIS_URL")

    # --- Redis client (lazy) ----------------------------------------------
    redis_client = None
    if rate_limit_enabled and effective_redis_url:
        try:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(
                effective_redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                retry_on_timeout=True,
            )
        except Exception:
            logger.warning(
                "Failed to initialise Redis for rate limiting; "
                "rate limiter will be disabled.",
                exc_info=True,
            )

    # --- Middleware stack (order matters: first added = outermost) ---------
    # 1. Request logging (outermost so it captures total time)
    app.add_middleware(RequestLoggingMiddleware)

    # 2. Request ID (must run before logging reads it)
    app.add_middleware(RequestIDMiddleware)

    # 3. Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # 4. Rate limiting
    app.add_middleware(
        RateLimitMiddleware,
        redis_client=redis_client,
        enabled=rate_limit_enabled,
        default_limit=rate_limit_default,
    )

    # 5. Input sanitisation (innermost, closest to route handlers)
    app.add_middleware(
        InputSanitizationMiddleware,
        max_request_size=max_request_size,
    )