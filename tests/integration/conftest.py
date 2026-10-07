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

import logging
import os

import httpx
import pytest

pytestmark = pytest.mark.integration

DEFAULT_API_URL = "http://localhost:8081"
DEFAULT_OPENSEARCH_URL = "http://localhost:9200"

logger = logging.getLogger("tests.integration")

# Methods that may be sent again: the server did nothing, or doing it twice is the same.
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class _ReconnectingTransport(httpx.HTTPTransport):
    """Resend an idempotent request once, on a fresh connection, when the server dropped
    the pooled keep-alive one (``RemoteProtocolError: Server disconnected``).

    A 500 in one teardown made the API close its connection, and every later request on
    the session client then failed with "Server disconnected": one leftover row turned
    into a row of teardown ERRORs for unrelated tests. httpx discards the dead connection,
    so the resend opens a new one. POSTs are never resent.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return super().handle_request(request)
        except (httpx.RemoteProtocolError, httpx.ReadError):
            if request.method not in _IDEMPOTENT:
                raise
            logger.warning("%s %s: server dropped the connection; resending once", request.method, request.url)
            return super().handle_request(request)


def cleanup(api_base_url: str, path: str) -> int | None:
    """DELETE ``path`` for teardown, never raising: returns the status, or None when the
    request failed outright. A refusal is logged, not raised, so one leftover cannot fail
    the test that made it or the ones after it. Its own short-lived client, so a broken
    session connection cannot affect it."""
    try:
        with httpx.Client(base_url=api_base_url, timeout=30.0, transport=_ReconnectingTransport()) as client:
            status = client.delete(path).status_code
    except httpx.HTTPError as exc:
        logger.warning("teardown DELETE %s failed: %s", path, exc)
        return None
    if status >= 400 and status != 404:
        logger.warning("teardown DELETE %s => %s; left in place", path, status)
    return status


def _reachable(url: str) -> bool:
    """True when something answers at `url`. Any HTTP reply counts — a 404 still
    proves a server is listening, and which routes exist is the tests' business."""
    import httpx

    try:
        httpx.get(url, timeout=3.0)
    except Exception:
        return False
    return True


# Fixtures that reach the live API. Tests that use none of them (the Moodle publish and
# vSphere lab tests run the API code in-process) carry their own configuration gates and
# must not be skipped for a missing API: the Moodle CI lane has no API stack at all.
_LIVE_API_FIXTURES = frozenset(
    {"api_base_url", "api_client", "async_api_client", "range_template", "make_range", "teardown_delete"}
)


def pytest_collection_modifyitems(config, items):
    """Skip live-API tests only when the API is not up — unless it is required.

    With INTEGRATION_REQUIRE_API=1 (set by scripts/itest.sh, which CI runs) a missing
    API is an error, not a skip: a required job that starts no stack must go red, not
    report "N skipped" as green.
    """
    needs_api = [i for i in items if _LIVE_API_FIXTURES.intersection(getattr(i, "fixturenames", ()))]
    if not needs_api:
        return
    api_url = os.getenv("API_BASE_URL", DEFAULT_API_URL)
    if _reachable(api_url):
        return
    if os.getenv("INTEGRATION_REQUIRE_API", "").strip().lower() in ("1", "true", "yes"):
        raise pytest.UsageError(
            f"INTEGRATION_REQUIRE_API is set but no API answers at {api_url}; "
            "start the stack (make itest) or set API_BASE_URL"
        )
    skip = pytest.mark.skip(reason=f"no API at {api_url} — start the stack or set API_BASE_URL")
    for item in needs_api:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Base URL for the live API server."""
    return os.getenv("API_BASE_URL", DEFAULT_API_URL)


@pytest.fixture(scope="session")
def api_client(api_base_url):
    """httpx client pointed at the live API. Recovers from a dropped keep-alive
    connection by resending idempotent requests once (_ReconnectingTransport)."""
    with httpx.Client(base_url=api_base_url, timeout=30.0, transport=_ReconnectingTransport()) as client:
        yield client


@pytest.fixture(scope="session")
def teardown_delete(api_base_url):
    """``teardown_delete(path)``: a DELETE for teardown that logs a refusal instead of
    raising, on its own connection (see ``cleanup``)."""
    return lambda path: cleanup(api_base_url, path)


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
def make_range(api_client, api_base_url, range_template):
    """Create a range and clean it up afterwards (logged, not raised, if refused)."""
    created: list[str] = []

    def _make(name: str) -> str:
        resp = api_client.post("/ranges", json={"name": name, "template_id": range_template})
        assert resp.status_code in (200, 201), f"POST /ranges => {resp.status_code} {resp.text}"
        rid = resp.json()["id"]
        created.append(rid)
        return rid

    yield _make

    for rid in created:
        cleanup(api_base_url, f"/ranges/{rid}")
