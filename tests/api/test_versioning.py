"""API versioning (app/versioning.py, docs/adr/0002): /api/v1 is real, root paths alias it."""

from __future__ import annotations

import pytest
from app.main import app
from app.versioning import CURRENT_VERSION, SERVER_PREFIX


@pytest.mark.parametrize("path", ["/health", "/v1/health", "/api/v1/health"])
def test_versioned_and_root_paths_reach_the_same_route(client, path):
    """Root path (existing clients), nginx-stripped /v1 and direct /api/v1 all resolve."""
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]
    assert resp.headers["X-API-Version"] == CURRENT_VERSION.value


def test_versioned_path_keeps_query_and_params(client):
    a = client.get("/ranges", params={"limit": 1})
    b = client.get("/api/v1/ranges", params={"limit": 1})
    assert a.status_code == b.status_code
    assert a.json() == b.json()


@pytest.mark.parametrize("path", ["/v9/health", "/api/v2/health"])
def test_unsupported_version_is_404_with_reason(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert "Unsupported API version" in resp.json()["detail"]


def test_bare_version_root_maps_to_root(client):
    assert client.get("/api/v1").status_code == client.get("/").status_code


def test_unknown_route_under_version_is_plain_404(client):
    resp = client.get("/api/v1/definitely-not-a-route")
    assert resp.status_code == 404
    assert "Unsupported" not in resp.text


def test_openapi_declares_versioned_server():
    assert app.openapi()["servers"] == [{"url": SERVER_PREFIX}]
    assert SERVER_PREFIX == "/api/v1"
