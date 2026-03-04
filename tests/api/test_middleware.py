"""Tests for TrueNorth Range security middleware."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from app.middleware import (
    InputSanitizationMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)


# ── Standalone test app with all middleware ────────────────────────────

def _make_app(redis_client=None, rate_limit_enabled=True, max_request_size=10 * 1024 * 1024):
    """Build a minimal FastAPI app with the full middleware stack."""
    _app = FastAPI()

    @_app.get("/health")
    def health():
        return {"status": "ok"}

    @_app.get("/items")
    def list_items():
        return [{"id": 1}]

    @_app.post("/items")
    async def create_item(request: Request):
        body = await request.body()
        return JSONResponse(
            {"size": len(body), "body": body.decode("utf-8", errors="replace")}
        )

    # Middleware stack (last added = outermost = runs first)
    _app.add_middleware(
        InputSanitizationMiddleware, max_request_size=max_request_size
    )
    _app.add_middleware(
        RateLimitMiddleware,
        redis_client=redis_client,
        enabled=rate_limit_enabled,
        default_limit=100,
    )
    _app.add_middleware(SecurityHeadersMiddleware)
    _app.add_middleware(RequestIDMiddleware)
    return _app


@pytest.fixture
def mw_client():
    """TestClient with security middleware (no Redis)."""
    return TestClient(_make_app(redis_client=None))


# ── Rate Limiting ──────────────────────────────────────────────────────

class TestRateLimit:
    def test_rate_limit_headers_present(self):
        """With Redis mock, responses carry X-RateLimit-* headers."""
        redis = MagicMock()
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[None, None, 1, None])
        redis.pipeline.return_value = pipe

        app = _make_app(redis_client=redis)
        c = TestClient(app)
        resp = c.get("/items")
        assert resp.status_code == 200
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers

    def test_rate_limit_not_applied_to_health(self):
        """/health is in the unlimited-paths list."""
        redis = MagicMock()
        app = _make_app(redis_client=redis)
        c = TestClient(app)
        resp = c.get("/health")
        assert resp.status_code == 200
        redis.pipeline.assert_not_called()

    def test_rate_limit_disabled_without_redis(self, mw_client):
        """Without Redis the limiter degrades gracefully (no 429, no headers)."""
        resp = mw_client.get("/items")
        assert resp.status_code == 200
        assert "x-ratelimit-limit" not in resp.headers


# ── Security Headers ──────────────────────────────────────────────────

class TestSecurityHeaders:
    _EXPECTED = [
        "x-content-type-options",
        "x-frame-options",
        "x-xss-protection",
        "content-security-policy",
        "referrer-policy",
        "permissions-policy",
    ]

    def test_security_headers_present(self, mw_client):
        """All six hardening headers must be on every response."""
        resp = mw_client.get("/items")
        for h in self._EXPECTED:
            assert h in resp.headers, f"Missing header: {h}"

    def test_server_header_stripped(self, mw_client):
        """Server header must not leak implementation details."""
        resp = mw_client.get("/items")
        assert "server" not in resp.headers

    def test_csp_header(self, mw_client):
        """Content-Security-Policy defaults to 'self'."""
        resp = mw_client.get("/items")
        assert resp.headers["content-security-policy"] == "default-src 'self'"


# ── Request ID ─────────────────────────────────────────────────────────

class TestRequestID:
    def test_request_id_generated(self, mw_client):
        """Every response must carry a valid UUID in X-Request-ID."""
        resp = mw_client.get("/items")
        rid = resp.headers.get("x-request-id", "")
        assert rid
        uuid.UUID(rid)  # must not raise

    def test_request_id_passed_through(self, mw_client):
        """A valid client-supplied X-Request-ID is echoed back."""
        fixed = str(uuid.uuid4())
        resp = mw_client.get("/items", headers={"X-Request-ID": fixed})
        assert resp.headers["x-request-id"] == fixed

    def test_invalid_request_id_replaced(self, mw_client):
        """A non-UUID X-Request-ID is replaced with a fresh UUID."""
        resp = mw_client.get("/items", headers={"X-Request-ID": "not-a-uuid"})
        rid = resp.headers["x-request-id"]
        assert rid != "not-a-uuid"
        uuid.UUID(rid)  # replacement is a valid UUID


# ── Input Sanitisation ─────────────────────────────────────────────────

class TestInputSanitization:
    def test_null_bytes_stripped(self, mw_client):
        """Null bytes in request body are silently removed."""
        resp = mw_client.post(
            "/items",
            content=b"hello\x00world",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "\x00" not in resp.json().get("body", "")

    def test_oversized_payload_rejected(self):
        """Payload exceeding max_request_size returns 413."""
        app = _make_app(max_request_size=100)
        c = TestClient(app)
        resp = c.post(
            "/items",
            content=b"x" * 200,
            headers={"Content-Type": "application/json", "Content-Length": "200"},
        )
        assert resp.status_code == 413

    def test_invalid_content_type(self, mw_client):
        """Unsupported Content-Type on POST returns 415."""
        resp = mw_client.post(
            "/items",
            content=b"<xml/>",
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code == 415