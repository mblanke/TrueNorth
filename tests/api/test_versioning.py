"""Tests for control-plane/api/app/versioning.py - API versioning support."""

from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure the control-plane package is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "control-plane", "api"))

from app.versioning import (
    DEPRECATED_VERSIONS,
    LATEST_VERSION,
    SUPPORTED_VERSIONS,
    APIVersion,
    V1toV2Adapter,
    VersionMiddleware,
    create_versioned_app,
)

# ---------------------------------------------------------------------------
# test_supported_versions
# ---------------------------------------------------------------------------


def test_supported_versions():
    """Validates the version constants are set correctly."""
    assert APIVersion.V1 in SUPPORTED_VERSIONS
    assert APIVersion.V2 in SUPPORTED_VERSIONS
    assert LATEST_VERSION == APIVersion.V2
    assert APIVersion.V1 in DEPRECATED_VERSIONS
    assert APIVersion.V2 not in DEPRECATED_VERSIONS


# ---------------------------------------------------------------------------
# test_version_middleware_adds_header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_middleware_adds_header():
    """VersionMiddleware must set the X-API-Version response header."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.add_middleware(VersionMiddleware)

    @app.get("/api/v2/test")
    async def _test_endpoint():
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/api/v2/test")
    assert resp.status_code == 200
    assert resp.headers.get("X-API-Version") == "v2"


# ---------------------------------------------------------------------------
# test_deprecated_version_warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecated_version_warning():
    """Deprecated v1 requests include Deprecation + Sunset headers."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.add_middleware(VersionMiddleware)

    @app.get("/api/v1/ranges")
    async def _v1_ranges():
        return {"ranges": []}

    client = TestClient(app)
    resp = client.get("/api/v1/ranges")
    assert resp.status_code == 200
    assert resp.headers.get("X-API-Version") == "v1"
    assert resp.headers.get("Deprecation") == "true"
    assert "Sunset" in resp.headers
    assert "Link" in resp.headers


# ---------------------------------------------------------------------------
# test_v1_to_v2_range_adapter
# ---------------------------------------------------------------------------


def test_v1_to_v2_range_adapter():
    """V1toV2Adapter.adapt_range_response adds v2-specific fields."""
    v1_data = {"id": "r-1", "name": "SOC Lab", "state": "ready"}
    v2_data = V1toV2Adapter.adapt_range_response(v1_data)

    # Original fields preserved
    assert v2_data["id"] == "r-1"
    assert v2_data["name"] == "SOC Lab"
    # v2 additions present
    assert "resource_usage" in v2_data
    assert "health_status" in v2_data
    assert "tags" in v2_data
    # Default values
    assert v2_data["resource_usage"] == {}
    assert v2_data["health_status"] == "unknown"
    assert v2_data["tags"] == []


# ---------------------------------------------------------------------------
# test_v1_to_v2_list_pagination
# ---------------------------------------------------------------------------


def test_v1_to_v2_list_pagination():
    """V1toV2Adapter.adapt_list_response wraps lists in pagination envelope."""
    result = V1toV2Adapter.adapt_list_response(
        items=[{"id": 1}, {"id": 2}],
        total=50,
        page=1,
        page_size=20,
    )

    assert result["items"] == [{"id": 1}, {"id": 2}]
    assert result["total"] == 50
    assert result["page"] == 1
    assert result["page_size"] == 20
    assert result["has_more"] is True

    # Last page
    result_last = V1toV2Adapter.adapt_list_response(
        items=[{"id": 50}],
        total=50,
        page=3,
        page_size=20,
    )
    assert result_last["has_more"] is False


# ---------------------------------------------------------------------------
# test_v1_to_v2_exercise_adapter
# ---------------------------------------------------------------------------


def test_v1_to_v2_exercise_adapter():
    """V1toV2Adapter.adapt_exercise_response adds v2 exercise fields."""
    v1_data = {"id": "ex-1", "name": "IR Drill", "score": 85}
    v2_data = V1toV2Adapter.adapt_exercise_response(v1_data)

    assert v2_data["id"] == "ex-1"
    assert "detailed_scores" in v2_data
    assert "timeline_events" in v2_data
    assert "recommendations" in v2_data


# ---------------------------------------------------------------------------
# test_create_versioned_app
# ---------------------------------------------------------------------------


def test_create_versioned_app():
    """create_versioned_app returns routers for both versions."""
    routers = create_versioned_app()
    assert APIVersion.V1 in routers
    assert APIVersion.V2 in routers
