"""Tests for the pluggable LMS backend system (Phase 1 — LMS modularity)."""

from __future__ import annotations

import httpx
import pytest
from app.lms import (
    NullLMSBackend,
    XAPILRSBackend,
    _reset_backend,  # noqa: PLC2701 — test-only helper
    get_lms_backend,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_lms_singleton():
    """Ensure every test starts with a fresh singleton."""
    _reset_backend()
    yield
    _reset_backend()


_SAMPLE_STMT = {
    "id": "test-id",
    "verb": {"id": "http://adlnet.gov/expapi/verbs/launched"},
    "actor": {"objectType": "Agent", "mbox": "mailto:a@b.com"},
    "object": {"objectType": "Activity", "id": "http://example.com/act/1"},
}


# ---------------------------------------------------------------------------
# NullLMSBackend
# ---------------------------------------------------------------------------


class TestNullLMSBackend:
    @pytest.mark.asyncio
    async def test_emit_statement_returns_true(self):
        backend = NullLMSBackend()
        result = await backend.emit_statement(_SAMPLE_STMT)
        assert result is True

    @pytest.mark.asyncio
    async def test_emit_statements_returns_zero(self):
        """Null backend reports 0 sent — nothing actually reaches an LRS."""
        backend = NullLMSBackend()
        result = await backend.emit_statements([_SAMPLE_STMT, _SAMPLE_STMT])
        assert result == 0

    def test_emit_statement_sync_returns_true(self):
        backend = NullLMSBackend()
        result = backend.emit_statement_sync(_SAMPLE_STMT)
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_true(self):
        backend = NullLMSBackend()
        assert await backend.health_check() is True

    def test_no_http_calls_made(self, mocker):
        """Null backend must never open a network connection."""
        mock_client = mocker.patch("httpx.Client")
        mock_async_client = mocker.patch("httpx.AsyncClient")
        backend = NullLMSBackend()
        backend.emit_statement_sync(_SAMPLE_STMT)
        mock_client.assert_not_called()
        mock_async_client.assert_not_called()


# ---------------------------------------------------------------------------
# XAPILRSBackend
# ---------------------------------------------------------------------------


class TestXAPILRSBackend:
    """Test HTTP transport against a mock LRS."""

    def _make_backend(self, lrs_url: str = "http://mock-lrs:8080", lrs_auth: str = "dGVzdA==") -> XAPILRSBackend:
        return XAPILRSBackend(lrs_url=lrs_url, lrs_auth=lrs_auth)

    @pytest.mark.asyncio
    async def test_emit_statement_success_200(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(return_value=httpx.Response(200))
        backend = self._make_backend()
        assert await backend.emit_statement(_SAMPLE_STMT) is True

    @pytest.mark.asyncio
    async def test_emit_statement_success_204(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(return_value=httpx.Response(204))
        backend = self._make_backend()
        assert await backend.emit_statement(_SAMPLE_STMT) is True

    @pytest.mark.asyncio
    async def test_emit_statement_lrs_500_returns_false(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        backend = self._make_backend()
        assert await backend.emit_statement(_SAMPLE_STMT) is False

    @pytest.mark.asyncio
    async def test_emit_statement_network_error_returns_false(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(side_effect=httpx.ConnectError("refused"))
        backend = self._make_backend()
        assert await backend.emit_statement(_SAMPLE_STMT) is False

    @pytest.mark.asyncio
    async def test_emit_statement_sends_correct_headers(self, respx_mock):
        route = respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(return_value=httpx.Response(200))
        backend = self._make_backend()
        await backend.emit_statement(_SAMPLE_STMT)

        request = route.calls.last.request
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["X-Experience-API-Version"] == "1.0.3"
        assert request.headers["Authorization"] == "Basic dGVzdA=="

    @pytest.mark.asyncio
    async def test_emit_statement_no_auth_header_when_empty(self, respx_mock):
        route = respx_mock.post("http://no-auth-lrs:8080/xapi/statements").mock(return_value=httpx.Response(200))
        backend = XAPILRSBackend(lrs_url="http://no-auth-lrs:8080", lrs_auth="")
        await backend.emit_statement(_SAMPLE_STMT)

        request = route.calls.last.request
        assert "Authorization" not in request.headers

    @pytest.mark.asyncio
    async def test_emit_statements_counts_successes(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(
            side_effect=[
                httpx.Response(200),
                httpx.Response(500),
                httpx.Response(204),
            ]
        )
        backend = self._make_backend()
        count = await backend.emit_statements([_SAMPLE_STMT, _SAMPLE_STMT, _SAMPLE_STMT])
        assert count == 2

    def test_emit_statement_sync_success(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(return_value=httpx.Response(200))
        backend = self._make_backend()
        assert backend.emit_statement_sync(_SAMPLE_STMT) is True

    def test_emit_statement_sync_failure_returns_false(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(return_value=httpx.Response(503))
        backend = self._make_backend()
        assert backend.emit_statement_sync(_SAMPLE_STMT) is False

    def test_emit_statement_sync_network_error_returns_false(self, respx_mock):
        respx_mock.post("http://mock-lrs:8080/xapi/statements").mock(side_effect=httpx.ConnectError("refused"))
        backend = self._make_backend()
        assert backend.emit_statement_sync(_SAMPLE_STMT) is False

    @pytest.mark.asyncio
    async def test_health_check_success(self, respx_mock):
        respx_mock.get("http://mock-lrs:8080/xapi/about").mock(
            return_value=httpx.Response(200, json={"version": ["1.0.3"]})
        )
        backend = self._make_backend()
        assert await backend.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_lrs_down(self, respx_mock):
        respx_mock.get("http://mock-lrs:8080/xapi/about").mock(side_effect=httpx.ConnectError("refused"))
        backend = self._make_backend()
        assert await backend.health_check() is False

    def test_reads_env_vars_when_no_explicit_args(self, monkeypatch):
        monkeypatch.setenv("LRS_URL", "http://env-lrs:9999")
        monkeypatch.setenv("LRS_AUTH", "envtoken")
        backend = XAPILRSBackend()
        assert backend._url == "http://env-lrs:9999"
        assert backend._auth == "envtoken"

    def test_strips_trailing_slash_from_url(self):
        backend = XAPILRSBackend(lrs_url="http://lrs:8080/")
        assert not backend._url.endswith("/")


# ---------------------------------------------------------------------------
# get_lms_backend factory
# ---------------------------------------------------------------------------


class TestGetLMSBackend:
    def test_default_returns_xapi_lrs(self, monkeypatch):
        monkeypatch.delenv("LMS_BACKEND", raising=False)
        backend = get_lms_backend()
        assert isinstance(backend, XAPILRSBackend)

    def test_explicit_xapi_lrs(self, monkeypatch):
        monkeypatch.setenv("LMS_BACKEND", "xapi_lrs")
        backend = get_lms_backend()
        assert isinstance(backend, XAPILRSBackend)

    def test_null_backend(self, monkeypatch):
        monkeypatch.setenv("LMS_BACKEND", "null")
        backend = get_lms_backend()
        assert isinstance(backend, NullLMSBackend)

    def test_unknown_backend_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("LMS_BACKEND", "nonexistent_backend")
        with pytest.raises(ValueError, match="Unknown LMS_BACKEND"):
            get_lms_backend()

    def test_singleton_caching(self, monkeypatch):
        monkeypatch.setenv("LMS_BACKEND", "null")
        b1 = get_lms_backend()
        b2 = get_lms_backend()
        assert b1 is b2

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LMS_BACKEND", "NULL")
        backend = get_lms_backend()
        assert isinstance(backend, NullLMSBackend)
