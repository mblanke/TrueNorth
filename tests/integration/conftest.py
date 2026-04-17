"""Fixtures for integration tests (require live services)."""

from __future__ import annotations

import os

import pytest

# Skip the entire integration suite when INTEGRATION_TEST is not set
pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests unless INTEGRATION_TEST env var is set."""
    if os.getenv("INTEGRATION_TEST"):
        return
    skip = pytest.mark.skip(reason="Set INTEGRATION_TEST=1 to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Base URL for the live API server."""
    return os.getenv("API_BASE_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def api_client(api_base_url):
    """httpx client pointed at the live API."""
    import httpx

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def async_api_client(api_base_url):
    """Async httpx client pointed at the live API."""
    import httpx

    async def _make():
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30.0) as client:
            yield client

    return _make


@pytest.fixture(scope="session")
def opensearch_url() -> str:
    return os.getenv("OPENSEARCH_URL", "http://localhost:9200")
