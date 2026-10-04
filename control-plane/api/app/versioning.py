"""TrueNorth Range - API versioning by URL prefix (docs/adr/0002-interface-versioning.md).

The published contract (docs/interfaces/openapi.json) is served under ``/api/v1``.
Routes are registered once, unprefixed; ``VersionPrefixMiddleware`` maps a versioned
path onto them. Behind nginx (which strips ``/api/``) a client's ``/api/v1/ranges``
arrives as ``/v1/ranges``; direct to uvicorn it arrives as ``/api/v1/ranges``. Both
resolve to ``/ranges``. Unversioned paths keep working as aliases of the current
version, so existing clients are unaffected.

A breaking change adds a new version here rather than changing v1. When a version is
retired, give it RFC 8594 ``Deprecation`` / ``Sunset`` headers for a full release first.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class APIVersion(str, Enum):
    V1 = "v1"


CURRENT_VERSION = APIVersion.V1
SUPPORTED_VERSIONS = frozenset(v.value for v in APIVersion)
SERVER_PREFIX = f"/api/{CURRENT_VERSION.value}"

# /v1, /v1/..., /api/v1, /api/v1/...  (group 1 = version, group 2 = remainder)
_VERSION_PATH_RE = re.compile(r"^(?:/api)?/(v\d+)(/.*)?$")


class VersionPrefixMiddleware:
    """Strip a supported version prefix and stamp ``X-API-Version`` on responses.

    Pure ASGI (not BaseHTTPMiddleware) so WebSockets and streaming responses pass
    through untouched. Register it last so it is outermost and every other middleware
    sees the canonical path.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        match = _VERSION_PATH_RE.match(scope["path"])
        if match:
            version, rest = match.group(1), match.group(2) or "/"
            if version not in SUPPORTED_VERSIONS:
                if scope["type"] == "http":
                    await _unsupported(version, send)
                return
            scope = dict(scope, path=rest, raw_path=rest.encode())

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-api-version", CURRENT_VERSION.value.encode()))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, send_with_version)


async def _unsupported(version: str, send: Send) -> None:
    body = json.dumps(
        {"detail": f"Unsupported API version: {version}. Supported: {sorted(SUPPORTED_VERSIONS)}"}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
