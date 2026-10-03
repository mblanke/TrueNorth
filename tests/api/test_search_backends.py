"""Tests for the pluggable Search/Telemetry backend system (Phase 3)."""

from __future__ import annotations

import httpx
import pytest
from app.search_backends import (
    NullSearchBackend,
    OpenSearchBackend,
    _reset_backend,  # noqa: PLC2701 — test-only helper
    get_search_backend,
)
from fastapi import HTTPException

_SAMPLE_EVENTS = [
    {"event_type": "login", "user": "alice", "@timestamp": "2025-01-01T00:00:00Z"},
    {"event_type": "rdp_connect", "src": "10.0.0.1"},
]

_MOCK_BULK_RESPONSE = {
    "errors": False,
    "items": [
        {"index": {"_index": "range-1", "_id": "a", "status": 201}},
        {"index": {"_index": "range-1", "_id": "b", "status": 201}},
    ],
}

_MOCK_SEARCH_RESPONSE = {
    "hits": {
        "total": {"value": 2},
        "hits": [{"_source": e} for e in _SAMPLE_EVENTS],
    }
}


@pytest.fixture(autouse=True)
def reset_search_singleton():
    _reset_backend()
    yield
    _reset_backend()


# ---------------------------------------------------------------------------
# NullSearchBackend
# ---------------------------------------------------------------------------


class TestNullSearchBackend:
    @pytest.mark.asyncio
    async def test_ingest_returns_zero(self):
        backend = NullSearchBackend()
        assert await backend.ingest("idx", _SAMPLE_EVENTS) == 0

    @pytest.mark.asyncio
    async def test_search_returns_empty_hits(self):
        backend = NullSearchBackend()
        result = await backend.search("idx", "*")
        assert result["hits"]["total"]["value"] == 0
        assert result["hits"]["hits"] == []

    @pytest.mark.asyncio
    async def test_health_check_returns_true(self):
        assert await NullSearchBackend().health_check() is True


# ---------------------------------------------------------------------------
# OpenSearchBackend
# ---------------------------------------------------------------------------


class TestOpenSearchBackend:
    def _make_backend(self) -> OpenSearchBackend:
        return OpenSearchBackend(url="http://mock-os:9200")

    @pytest.mark.asyncio
    async def test_ingest_success_counts_accepted(self, respx_mock):
        respx_mock.post("http://mock-os:9200/_bulk").mock(return_value=httpx.Response(200, json=_MOCK_BULK_RESPONSE))
        backend = self._make_backend()
        count = await backend.ingest("range-1", _SAMPLE_EVENTS)
        assert count == 2

    @pytest.mark.asyncio
    async def test_ingest_network_error_returns_zero(self, respx_mock):
        respx_mock.post("http://mock-os:9200/_bulk").mock(side_effect=httpx.ConnectError("refused"))
        backend = self._make_backend()
        assert await backend.ingest("range-1", _SAMPLE_EVENTS) == 0

    @pytest.mark.asyncio
    async def test_ingest_empty_events_returns_zero(self, respx_mock):
        backend = self._make_backend()
        assert await backend.ingest("range-1", []) == 0
        # No HTTP call should have been made
        assert not respx_mock.calls

    @pytest.mark.asyncio
    async def test_ingest_sends_ndjson_content_type(self, respx_mock):
        route = respx_mock.post("http://mock-os:9200/_bulk").mock(
            return_value=httpx.Response(200, json=_MOCK_BULK_RESPONSE)
        )
        await self._make_backend().ingest("idx", _SAMPLE_EVENTS)
        assert route.calls.last.request.headers["Content-Type"] == "application/x-ndjson"

    @pytest.mark.asyncio
    async def test_search_success(self, respx_mock):
        respx_mock.post("http://mock-os:9200/range-1/_search").mock(
            return_value=httpx.Response(200, json=_MOCK_SEARCH_RESPONSE)
        )
        result = await self._make_backend().search("range-1", "event_type:login")
        assert result["hits"]["total"]["value"] == 2

    @pytest.mark.asyncio
    async def test_search_backend_error_raises_502(self, respx_mock):
        respx_mock.post("http://mock-os:9200/range-1/_search").mock(
            return_value=httpx.Response(500, text="Internal Error")
        )
        with pytest.raises(HTTPException) as exc_info:
            await self._make_backend().search("range-1", "*")
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_search_network_error_raises_502(self, respx_mock):
        respx_mock.post("http://mock-os:9200/range-1/_search").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(HTTPException) as exc_info:
            await self._make_backend().search("range-1", "*")
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_health_check_success(self, respx_mock):
        respx_mock.get("http://mock-os:9200/_cluster/health").mock(
            return_value=httpx.Response(200, json={"status": "green"})
        )
        assert await self._make_backend().health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_unreachable_returns_false(self, respx_mock):
        respx_mock.get("http://mock-os:9200/_cluster/health").mock(side_effect=httpx.ConnectError("refused"))
        assert await self._make_backend().health_check() is False

    def test_strips_trailing_slash(self):
        b = OpenSearchBackend(url="http://os:9200/")
        assert not b._url.endswith("/")

    def test_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENSEARCH_URL", "http://env-os:1234")
        b = OpenSearchBackend()
        assert b._url == "http://env-os:1234"


# ---------------------------------------------------------------------------
# get_search_backend factory
# ---------------------------------------------------------------------------


class TestGetSearchBackend:
    def test_default_returns_opensearch(self, monkeypatch):
        monkeypatch.delenv("SEARCH_BACKEND", raising=False)
        assert isinstance(get_search_backend(), OpenSearchBackend)

    def test_null_backend(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND", "null")
        assert isinstance(get_search_backend(), NullSearchBackend)

    def test_unknown_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND", "splunk_magic")
        with pytest.raises(ValueError, match="Unknown SEARCH_BACKEND"):
            get_search_backend()

    def test_singleton_caching(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND", "null")
        assert get_search_backend() is get_search_backend()

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND", "NULL")
        assert isinstance(get_search_backend(), NullSearchBackend)
