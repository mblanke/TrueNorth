"""TrueNorth as a browser meets it, for tests/integration/test_moodle_student_journey.py.

One ASGI app on one origin, laid out as the web container's nginx lays it out
(control-plane/web/nginx.conf): the production SPA build at ``/`` (unknown paths fall back
to ``index.html``, as ``try_files`` does) and the API under ``/api``, with the prefix
stripped before the API sees the path, as ``proxy_pass`` does. Run by the test in its own
process (``uvicorn _journey_server:app``), on its own SQLite file, with real token checks:
``AUTH_BACKEND=generic_oidc`` against the key set served at ``/test-idp/jwks``. The
browser's Keycloak calls are answered by the test with tokens signed by that key, so the
API verifies signature, issuer, audience and expiry as it would Keycloak's.

Test-only, never shipped. What differs from a deployment:
- ``app.net_guard`` may reach loopback, where the disposable Moodle is published (the same
  exception test_moodle_cmi5_lti.py makes in-process); every other rule stays.
- No nginx: no TLS, no edge security headers, no rate limits.

Environment: JOURNEY_SPA_DIST (the build's ``browser`` directory), JOURNEY_IDP_JWKS (a JWKS
file), plus the API's own settings (DATABASE_URL, OIDC_*, LTI_*, LRS_*), set by the test.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from app import net_guard  # patched below, before the API is imported
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

_vetted = net_guard.refused
net_guard.refused = lambda addr, *, allow_private: not addr.is_loopback and _vetted(addr, allow_private=allow_private)
# The guard pins the first address a name resolves to; "localhost" may be ::1 first, and the
# Moodle compose file publishes on 127.0.0.1 only.
net_guard._resolve = lambda host, port, **kw: socket.getaddrinfo(host, port, socket.AF_INET, **kw)

from app.main import app as api  # noqa: E402

SPA = Path(os.environ["JOURNEY_SPA_DIST"])
JWKS = json.loads(Path(os.environ["JOURNEY_IDP_JWKS"]).read_text())


class SpaFiles(StaticFiles):
    """The SPA's files; any other path is a client-side route, so it gets ``index.html``."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return FileResponse(SPA / "index.html")


spa = SpaFiles(directory=SPA)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        return await api(scope, receive, send)  # the API's own startup and shutdown
    path = scope.get("path", "")
    if path == "/api" or path.startswith("/api/"):
        inner = dict(scope)
        inner["path"] = path[4:] or "/"
        raw = scope.get("raw_path") or path.encode()
        inner["raw_path"] = raw[4:] or b"/"
        return await api(inner, receive, send)
    if path == "/test-idp/jwks":
        return await JSONResponse(JWKS)(scope, receive, send)
    return await spa(scope, receive, send)
