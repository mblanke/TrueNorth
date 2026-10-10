"""TrueNorth Range — Abstract LMS/LRS backend interface.

Any compliant backend must implement this ABC.  The transport layer is
entirely the backend's concern; callers (xapi.py, events.py, routers)
only interact with this interface.

Swapping backends is an env-var change (LMS_BACKEND=<name>), not a
code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LRSUnsupportedError(RuntimeError):
    """The configured backend cannot do this (e.g. the ``null`` backend has no LRS to read)."""


class LRSUnavailableError(RuntimeError):
    """The LRS could not be reached or timed out. Never reported as success."""


@dataclass
class LRSResponse:
    """What the LRS answered to one xAPI resource request, passed back unchanged."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names

    def json(self):
        import json

        return json.loads(self.body or b"null")


class BaseLMSBackend(ABC):
    """Minimal interface every LMS/LRS backend must satisfy."""

    # ------------------------------------------------------------------
    # Optional capability: the xAPI resource API (statements queries, State and
    # Agent Profile documents). cmi5 launching (app/cmi5) and the legacy re-issue
    # tool need it; plain emission does not. A backend without an LRS behind it
    # says so instead of pretending.
    # ------------------------------------------------------------------

    supports_resources: bool = False

    def xapi_request(
        self,
        method: str,
        resource: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        credential: str | None = None,
    ) -> LRSResponse:
        """Send one request to ``<LRS>/xapi/<resource>`` with the server's own credential, or
        with ``credential`` (base64 ``key:secret``) when the caller names another one.

        Raises ``LRSUnsupportedError`` when the backend has no LRS, and
        ``LRSUnavailableError`` when the LRS does not answer. Any HTTP status the LRS
        does answer with is returned, not raised: the caller decides what it means.
        """
        raise LRSUnsupportedError(f"{type(self).__name__} has no xAPI resource API")

    # ------------------------------------------------------------------
    # Statement emission
    # ------------------------------------------------------------------

    @abstractmethod
    async def emit_statement(self, statement: dict) -> bool:
        """Send a single xAPI statement.

        Must never raise — return False on any failure so callers can
        treat LRS emission as best-effort advisory traffic.
        """
        ...

    @abstractmethod
    async def emit_statements(self, statements: list[dict]) -> int:
        """Send multiple xAPI statements.

        Returns the count of successfully accepted statements.
        """
        ...

    @abstractmethod
    def emit_statement_sync(self, statement: dict, timeout: float = 2.0) -> bool:
        """Send a single xAPI statement synchronously.

        Intended for use from FastAPI BackgroundTasks or Celery workers
        where an async context is not available.  Must never raise.
        """
        ...

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and accepting statements."""
        ...
