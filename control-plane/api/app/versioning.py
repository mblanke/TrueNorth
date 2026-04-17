"""TrueNorth Range - API versioning via URL prefix and headers.

Supports:
  - URL prefix: /api/v1/ranges, /api/v2/ranges
  - Header: X-API-Version: 2
  - Default: v1 for backward compatibility
"""

from __future__ import annotations

import re
from enum import Enum

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# -- Version Enum ---------------------------------------------------------


class APIVersion(str, Enum):
    """Supported API versions."""

    V1 = "v1"
    V2 = "v2"


LATEST_VERSION = APIVersion.V2
SUPPORTED_VERSIONS = {APIVersion.V1, APIVersion.V2}
DEPRECATED_VERSIONS = {APIVersion.V1}  # V1 will be removed in future

# Pattern: /api/v1/..., /api/v2/...
_VERSION_URL_RE = re.compile(r"^/api/(v\d+)/")

# Sunset date for deprecated versions (ISO-8601)
_DEPRECATION_SUNSET = "2027-01-01T00:00:00Z"


# -- Middleware ------------------------------------------------------------


class VersionMiddleware(BaseHTTPMiddleware):
    """Adds ``X-API-Version`` header to responses and warns on deprecated versions.

    Version detection order:
      1. URL prefix ``/api/v1/...`` or ``/api/v2/...``
      2. ``X-API-Version`` request header (e.g. ``1`` or ``v1``)
      3. Default: **v1** for backward compatibility
    """

    async def dispatch(self, request: Request, call_next):
        version = self._detect_version(request)

        # Reject unsupported versions
        if version is not None and version not in SUPPORTED_VERSIONS:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"Unsupported API version: {version}. Supported: {sorted(v.value for v in SUPPORTED_VERSIONS)}"
                    ),
                },
            )

        resolved: APIVersion = version or APIVersion.V1

        # Store on request state so downstream handlers can inspect
        request.state.api_version = resolved

        response = await call_next(request)

        # Standard version header
        response.headers["X-API-Version"] = resolved.value

        # Deprecation headers (RFC 8594)
        if resolved in DEPRECATED_VERSIONS:
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = _DEPRECATION_SUNSET
            response.headers["Link"] = f'</api/{LATEST_VERSION.value}/>; rel="successor-version"'

        return response

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _detect_version(request: Request) -> APIVersion | None:
        """Return detected version or ``None`` (caller applies default)."""
        # 1. URL prefix
        match = _VERSION_URL_RE.match(request.url.path)
        if match:
            raw = match.group(1).lower()
            return _parse_version(raw)

        # 2. Header
        header = request.headers.get("X-API-Version", "").strip()
        if header:
            raw = header if header.startswith("v") else f"v{header}"
            return _parse_version(raw.lower())

        return None


def _parse_version(raw: str) -> APIVersion | None:
    """Safely convert raw string to :class:`APIVersion`."""
    try:
        return APIVersion(raw)
    except ValueError:
        return raw  # type: ignore[return-value]  # let middleware reject


# -- Versioned Router Factory ----------------------------------------------


def create_versioned_app() -> dict[APIVersion, APIRouter]:
    """Create versioned routers.

    Returns dict of version -> APIRouter.
    v2 routers can import and modify v1 behavior.

    Usage::

        routers = create_versioned_app()
        app.include_router(routers[APIVersion.V1], prefix="/api/v1")
        app.include_router(routers[APIVersion.V2], prefix="/api/v2")
    """
    v1_router = APIRouter(tags=["v1"])
    v2_router = APIRouter(tags=["v2"])

    # -- v1 placeholder routes ---------------------------------------------
    @v1_router.get("/ranges", summary="List ranges (v1)")
    async def list_ranges_v1():
        return {"ranges": [], "message": "v1 endpoint"}

    # -- v2 routes: extends / adapts v1 ------------------------------------
    @v2_router.get("/ranges", summary="List ranges (v2)")
    async def list_ranges_v2():
        return V1toV2Adapter.adapt_list_response(
            items=[],
            total=0,
            page=1,
            page_size=20,
        )

    return {
        APIVersion.V1: v1_router,
        APIVersion.V2: v2_router,
    }


# -- Migration Adapters ----------------------------------------------------


class V1toV2Adapter:
    """Adapter for migrating v1 response formats to v2.

    v2 enriches payloads with operational metadata and wraps collections
    in a pagination envelope.
    """

    @staticmethod
    def adapt_range_response(v1_response: dict) -> dict:
        """v2 adds: ``resource_usage``, ``health_status``, ``tags``."""
        return {
            **v1_response,
            "resource_usage": v1_response.get("resource_usage", {}),
            "health_status": v1_response.get("health_status", "unknown"),
            "tags": v1_response.get("tags", []),
        }

    @staticmethod
    def adapt_exercise_response(v1_response: dict) -> dict:
        """v2 adds: ``detailed_scores``, ``timeline_events``, ``recommendations``."""
        return {
            **v1_response,
            "detailed_scores": v1_response.get("detailed_scores", {}),
            "timeline_events": v1_response.get("timeline_events", []),
            "recommendations": v1_response.get("recommendations", []),
        }

    @staticmethod
    def adapt_list_response(
        items: list,
        total: int,
        page: int,
        page_size: int,
    ) -> dict:
        """v2 wraps lists in pagination envelope.

        ``{items, total, page, page_size, has_more}``
        """
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": (page * page_size) < total,
        }
