"""
TrueNorth Range — Security Hardening Middleware
================================================
Comprehensive FastAPI middleware providing:
  - CSRF protection (double-submit cookie)
  - Sliding-window rate limiting (Redis ZSET)
  - Security headers
  - Request-ID propagation
  - Structured JSON request logging
  - Input sanitisation
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextvars import ContextVar
from typing import Any

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
# 0. CSRF PROTECTION (double-submit cookie)
# =========================================================================

_CSRF_COOKIE = "truenorth_csrf"
_CSRF_HEADER = "x-csrf-token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_SECRET = os.getenv("CSRF_SECRET", secrets.token_hex(32))
_CSRF_TOKEN_TTL = 86400  # 1 day


def _generate_csrf_token() -> str:
    """Generate a signed CSRF token: payload.signature."""
    payload = secrets.token_hex(16)
    sig = hmac.new(_CSRF_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{sig}"


def _verify_csrf_token(token: str) -> bool:
    """Verify the CSRF token signature is valid."""
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    payload, sig = parts
    expected = hmac.new(_CSRF_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


class CsrfMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    On every response, sets a ``truenorth_csrf`` cookie with a signed token.
    On state-changing requests (POST/PUT/PATCH/DELETE), the ``X-CSRF-Token``
    header must match the cookie value.  Skipped when ``AUTH_DISABLED=true``.
    """

    def __init__(self, app: ASGIApp, enabled: bool = True) -> None:
        super().__init__(app)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled:
            return await call_next(request)

        method = request.method

        # Validate on mutating methods
        if method not in _CSRF_SAFE_METHODS:
            cookie_token = request.cookies.get(_CSRF_COOKIE, "")
            header_token = request.headers.get(_CSRF_HEADER, "")

            if (
                not cookie_token
                or not header_token
                or not hmac.compare_digest(cookie_token, header_token)
                or not _verify_csrf_token(header_token)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token missing or invalid"},
                )

        response = await call_next(request)

        # Always set / refresh the CSRF cookie so the frontend can read it
        if _CSRF_COOKIE not in request.cookies or method in _CSRF_SAFE_METHODS:
            token = _generate_csrf_token()
            response.set_cookie(
                key=_CSRF_COOKIE,
                value=token,
                max_age=_CSRF_TOKEN_TTL,
                httponly=False,  # JS must read it to send in header
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )

        return response


# =========================================================================
# 1. RATE LIMITING (Redis sliding-window via ZSET)
# =========================================================================

# Per-route overrides: (method, path_prefix) -> requests/min
_DEFAULT_ROUTE_LIMITS: dict[tuple[str, str], int] = {
    ("POST", "/ranges/batch-provision"): 5,
    ("POST", "/ranges"): 30,
    ("GET", ""): 200,  # all GET endpoints
}

_UNLIMITED_PATHS: list[str] = ["/health"]


def _match_route_limit(method: str, path: str, default: int) -> tuple[int, str]:
    """Return the (rate-limit, bucket) for a given method+path.

    The bucket identifies *which* limit was matched, and has to be part of the counter
    key. A per-route limit counted against a shared per-client key is not a per-route
    limit at all: every request lands in one tally, and each one is compared against
    whichever route's ceiling it happened to match, so the strictest configured route
    silently becomes the client's global ceiling. Concretely, 31 reads — well inside the
    200/min GET budget — used to make `POST /ranges` return 429, and the 5/min on
    `batch-provision` was tripped by any five requests of any kind.
    """
    for (m, prefix), limit in _DEFAULT_ROUTE_LIMITS.items():
        if method == m and (prefix == "" or path.startswith(prefix)):
            return limit, f"{m}:{prefix}"
    return default, "default"


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
    def _client_key(request: Request, bucket: str = "default") -> str:
        """Key by tenant_id (if authenticated) else client IP, per rate-limit bucket.

        `bucket` scopes the counter to the route whose limit is being applied — without
        it the configured per-route numbers are all enforced against one shared tally.
        See `_match_route_limit`.
        """
        tenant = getattr(request.state, "tenant_id", None)
        if tenant:
            return f"rl:tenant:{tenant}:{bucket}"
        from .client_address import client_ip

        # X-Forwarded-For only from a trusted proxy: otherwise every request could name a
        # fresh address and never reach a limit.
        ip = client_ip(request) or "unknown"
        return f"rl:ip:{ip}:{bucket}"

    async def _check_rate_limit(self, key: str, limit: int, now: float) -> tuple[bool, int, int]:
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled or self.redis is None:
            return await call_next(request)

        path = request.url.path
        method = request.method

        # Unlimited endpoints
        if any(path.startswith(p) for p in _UNLIMITED_PATHS):
            return await call_next(request)

        limit, bucket = _match_route_limit(method, path, self.default_limit)
        key = self._client_key(request, bucket)
        now = time.time()

        try:
            allowed, remaining, reset_at = await self._check_rate_limit(key, limit, now)
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        for header, value in self.HEADERS.items():
            response.headers[header] = value

        # HSTS only when the connection is (or was) over TLS
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if incoming and _UUID_RE.match(incoming) else str(uuid.uuid4())

        # Store in context var for downstream consumers
        request_id_ctx.set(rid)
        request.state.request_id = rid

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


# =========================================================================
# 4. REQUEST LOGGING
# =========================================================================

_QUIET_PATHS: list[str] = ["/health", "/metrics"]
_SLOW_THRESHOLD_MS: float = 1000.0

# Paths that carry a bearer secret. They are logged with the secret replaced, here and in
# uvicorn's access log (RedactSecretPaths). nginx skips logging them (access_log off).
_SECRET_PATH = re.compile(r"(/schedule/feed/)[^/?\s\"]+")


def redact_path(path: str) -> str:
    """Hide the calendar-feed token (ADR 0004) in anything about to be logged."""
    return _SECRET_PATH.sub(r"\1<redacted>", path)


class RedactSecretPaths(logging.Filter):
    """Logging filter for ``uvicorn.access``, whose records carry the request path in args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_path(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact_path(record.msg)
        return True


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured JSON logging of every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = redact_path(request.url.path)
        verbose = not any(path.startswith(qp) for qp in _QUIET_PATHS)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        rid = request_id_ctx.get("")
        tid = tenant_id_ctx.get("")
        uid = user_id_ctx.get("")

        log_entry: dict[str, Any] = {
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
# File uploads (multipart) get their own cap: ticket attachments and range documents
# allow 25 MB per file, and the web nginx passes up to 50M on /api/.
_MAX_UPLOAD_SIZE_DEFAULT = 50 * 1024 * 1024  # 50 MB

# Null bytes are stripped from every body except binary ones. A file upload is binary
# by nature (PNG, PDF, pcap and zip files all contain zero bytes), and stripping them
# silently corrupted every one while still answering 201.
_BINARY_CONTENT_TYPES = {"multipart/form-data", "application/octet-stream"}

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
        max_upload_size: int = _MAX_UPLOAD_SIZE_DEFAULT,
    ) -> None:
        super().__init__(app)
        self.max_request_size = max_request_size
        self.max_upload_size = max(max_upload_size, max_request_size)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method

        ct = request.headers.get("content-type", "")
        base_ct = ct.split(";")[0].strip().lower()

        # --- Payload size guard -------------------------------------------
        limit = self.max_upload_size if base_ct == "multipart/form-data" else self.max_request_size
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": (f"Payload too large. Max allowed: {limit} bytes.")},
            )

        # --- Content-Type validation on mutating methods ------------------
        if method in _METHODS_REQUIRING_CONTENT_TYPE and ct and base_ct not in _ALLOWED_CONTENT_TYPES:
            return JSONResponse(
                status_code=415,
                content={
                    "detail": (
                        f"Unsupported Content-Type: {base_ct}. Allowed: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}"
                    )
                },
            )

        # --- Null-byte stripping, except binary bodies (via receive wrapper) -
        if base_ct in _BINARY_CONTENT_TYPES:
            return await call_next(request)
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
    redis_url: str | None = None,
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
    MAX_UPLOAD_SIZE : str
        Maximum multipart (file upload) body in bytes (default ``"52428800"`` = 50 MB).
    REDIS_URL : str
        Fallback redis URL if *redis_url* parameter is not provided.
    """

    # --- Resolve configuration from env -----------------------------------
    rate_limit_enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    rate_limit_default = int(os.getenv("RATE_LIMIT_DEFAULT", "100"))
    max_request_size = int(os.getenv("MAX_REQUEST_SIZE", str(_MAX_REQUEST_SIZE_DEFAULT)))
    max_upload_size = int(os.getenv("MAX_UPLOAD_SIZE", str(_MAX_UPLOAD_SIZE_DEFAULT)))
    effective_redis_url = redis_url or os.getenv("REDIS_URL")
    csrf_enabled = os.getenv("AUTH_DISABLED", "false").lower() != "true"

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
                "Failed to initialise Redis for rate limiting; rate limiter will be disabled.",
                exc_info=True,
            )

    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecretPaths) for f in access_log.filters):
        access_log.addFilter(RedactSecretPaths())

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
        max_upload_size=max_upload_size,
    )

    # 6. CSRF protection (innermost: only reached after sanitisation)
    app.add_middleware(CsrfMiddleware, enabled=csrf_enabled)
