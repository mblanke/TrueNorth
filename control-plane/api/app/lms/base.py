"""TrueNorth Range — Abstract LMS/LRS backend interface.

Any compliant backend must implement this ABC.  The transport layer is
entirely the backend's concern; callers (xapi.py, events.py, routers)
only interact with this interface.

Swapping backends is an env-var change (LMS_BACKEND=<name>), not a
code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseLMSBackend(ABC):
    """Minimal interface every LMS/LRS backend must satisfy."""

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
