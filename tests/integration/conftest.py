"""Fixtures for integration tests (require live services).

These run against a real API and a real OpenSearch, so they are gated — but on whether
the services are actually reachable, not on a flag someone has to remember to set. The
old `INTEGRATION_TEST=1` gate hid staleness rather than absence: the suite sat green at
"27 skipped" for long enough that the tests drifted off the API contract entirely
(posting to `/telemetry/events` when the route is `/telemetry/{range_id}/events`,
creating a range with a null `template_id` the schema requires). A probe that skips only
when nothing is listening keeps that from happening again — when the stack is up, the
tests run, and drift shows up as a failure the same day it is introduced.

Point them at a non-default host with `API_BASE_URL` / `OPENSEARCH_URL`. Note this
repo publishes the API on 8081 (`8080/tcp -> 127.0.0.1:8081`), which is why the old
`localhost:8080` default never connected.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

DEFAULT_API_URL = "http://localhost:8081"
DEFAULT_OPENSEARCH_URL = "http://localhost:9200"


def _reachable(url: str) -> bool:
    """True when something answers at `url`. Any HTTP reply counts — a 404 still
    proves a server is listening, and which routes exist is the tests' business."""
    import httpx

    try:
        httpx.get(url, timeout=3.0)
    except Exception:
        return False
    return True


def pytest_collection_modifyitems(config, items):
    """Skip the suite only when the services it needs are not up."""
    api_url = os.getenv("API_BASE_URL", DEFAULT_API_URL)
    if _reachable(api_url):
        return
    skip = pytest.mark.skip(
        reason=f"no API at {api_url} — start the stack or set API_BASE_URL"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Base URL for the live API server."""
    return os.getenv("API_BASE_URL", DEFAULT_API_URL)


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
    return os.getenv("OPENSEARCH_URL", DEFAULT_OPENSEARCH_URL)


@pytest.fixture(scope="session")
def range_template(api_client):
    """A template to hang test ranges off.

    `RangeIn.template_id` is required and non-nullable, so a range cannot be created
    without one — the reason every lifecycle test used to 422 on its first call.
    """
    name = "integration-test-template"
    existing = api_client.get("/templates", params={"limit": 200})
    if existing.status_code == 200:
        items = existing.json()
        items = items.get("items", items) if isinstance(items, dict) else items
        for t in items or []:
            if t.get("name") == name:
                return t["id"]

    resp = api_client.post(
        "/templates",
        json={
            "name": name,
            "yaml": "name: integration-test-template\nenvironment: enterprise\n",
            "is_public": False,
        },
    )
    assert resp.status_code in (200, 201), f"POST /templates => {resp.status_code} {resp.text}"
    return resp.json()["id"]


@pytest.fixture
def make_range(api_client, range_template):
    """Create a range and clean it up afterwards."""
    created: list[str] = []

    def _make(name: str) -> str:
        resp = api_client.post("/ranges", json={"name": name, "template_id": range_template})
        assert resp.status_code in (200, 201), f"POST /ranges => {resp.status_code} {resp.text}"
        rid = resp.json()["id"]
        created.append(rid)
        return rid

    yield _make

    for rid in created:
        api_client.delete(f"/ranges/{rid}")
