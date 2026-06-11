"""TrueNorth Range — Null (no-op) LMS backend.

Use this when:
  - No LRS is deployed (offline / air-gapped ranges without xAPI)
  - Integration testing where LRS calls would fail or add noise
  - Feature-flag disabling of xAPI while preserving all call sites

Set LMS_BACKEND=null to activate.

All methods return success indicators (True / 0) so callers behave
as if statements were accepted, preserving normal flow.
"""

from __future__ import annotations

import logging

from .base import BaseLMSBackend

logger = logging.getLogger("truenorth.lms.null")


class NullLMSBackend(BaseLMSBackend):
    """Swallows all xAPI statements. Logs at DEBUG level."""

    async def emit_statement(self, statement: dict) -> bool:
        logger.debug("NullLMSBackend: drop statement verb=%s", statement.get("verb", {}).get("id"))
        return True

    async def emit_statements(self, statements: list[dict]) -> int:
        logger.debug("NullLMSBackend: drop %d statements", len(statements))
        return 0

    def emit_statement_sync(self, statement: dict, timeout: float = 2.0) -> bool:
        logger.debug("NullLMSBackend: drop sync statement verb=%s", statement.get("verb", {}).get("id"))
        return True

    async def health_check(self) -> bool:
        return True
