"""Every API client of OpenSearch authenticates and verifies TLS the same way.

Production runs the OpenSearch security plugin (TLS + basic auth). The search backend
sent credentials but never a CA, and the vector store and the /health check sent neither,
so turning security on would have broken them silently. search_backends/connection.py is
now the one place; these hold it and each client's use of it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from app import health
from app.search_backends import connection
from app.search_backends.opensearch import OpenSearchBackend
from app.vector_backends.opensearch import OpenSearchVectorStore


@pytest.fixture
def secured(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    for k, v in {"OPENSEARCH_URL": "https://opensearch:9200", "OPENSEARCH_USER": "admin",
                 "OPENSEARCH_PASS": "s3cret", "OPENSEARCH_VERIFY_SSL": str(ca)}.items():
        monkeypatch.setenv(k, v)
    return str(ca)


@pytest.mark.parametrize(("raw", "want"), [("true", True), ("", True), ("1", True), ("false", False),
                                           ("off", False), ("/etc/truenorth/opensearch-ca.pem",
                                                            "/etc/truenorth/opensearch-ca.pem")])
def test_verify_setting(monkeypatch, raw, want):
    monkeypatch.setenv("OPENSEARCH_VERIFY_SSL", raw)
    assert connection.verify_setting() == want


def test_verifies_by_default_and_sends_no_auth_without_a_user(monkeypatch):
    for k in ("OPENSEARCH_VERIFY_SSL", "OPENSEARCH_USER", "OPENSEARCH_PASS"):
        monkeypatch.delenv(k, raising=False)
    assert connection.client_kwargs(5) == {"timeout": 5, "verify": True}


def test_client_kwargs_carry_credentials_and_the_ca(secured):
    assert connection.client_kwargs(5) == {"timeout": 5, "verify": secured, "auth": ("admin", "s3cret")}


class _Capture:
    """httpx.AsyncClient stand-in that records its kwargs and answers 200."""

    seen: list[dict] = []

    def __init__(self, **kwargs):
        _Capture.seen.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return httpx.Response(200, json={"status": "green", "cluster_name": "c",
                                         "x": {"mappings": {"properties": {"embedding": {"dimension": 4}}}}},
                              request=httpx.Request("GET", url))


@pytest.fixture
def capture(monkeypatch):
    _Capture.seen = []
    monkeypatch.setattr(httpx, "AsyncClient", _Capture)
    return _Capture.seen


def test_search_backend_vector_store_and_health_all_use_it(secured, capture):
    asyncio.run(OpenSearchBackend().health_check())
    asyncio.run(OpenSearchVectorStore().index_dim("x"))
    asyncio.run(health.HealthChecker()._check_opensearch())
    assert len(capture) == 3
    for kw in capture:
        assert kw["auth"] == ("admin", "s3cret") and kw["verify"] == secured


def test_explicit_backend_credentials_still_win(secured, capture):
    asyncio.run(OpenSearchBackend(username="other", password="pw").health_check())
    assert capture[0]["auth"] == ("other", "pw") and capture[0]["verify"] == secured


def test_vector_store_reads_the_url_when_built_not_at_import(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_URL", "https://elsewhere:9200/")
    assert OpenSearchVectorStore().url == "https://elsewhere:9200"


def test_wrong_credentials_are_unhealthy_not_degraded(secured, respx_mock):
    respx_mock.get("https://opensearch:9200/_cluster/health").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"}))
    assert asyncio.run(OpenSearchBackend().health_check()) is False
    assert asyncio.run(health.HealthChecker()._check_opensearch()).status == health.HealthStatus.UNHEALTHY
