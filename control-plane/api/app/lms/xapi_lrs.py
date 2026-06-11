"""TrueNorth Range — xAPI LRS backend.

Works with any xAPI 1.0.3-compliant LRS:
  - yetanalytics/lrsql (default)
  - OpenDash 360
  - Ralph (SCORM Cloud)
  - Any other compliant endpoint

Configuration (env vars):
  LRS_URL   — base URL, e.g. http://lrs:8080   (default: http://lrs:8080)
  LRS_AUTH  — Base64-encoded Basic auth token  (default: "")
"""

from __future__ import annotations

import logging
import os

import httpx

from .base import BaseLMSBackend

logger = logging.getLogger("truenorth.lms.xapi_lrs")

_XAPI_VERSION_HEADER = "1.0.3"


class XAPILRSBackend(BaseLMSBackend):
    """xAPI 1.0.3-compliant LRS transport.

    Drop-in replacement target: change LRS_URL to point at any
    xAPI-compliant endpoint.  No code changes required.
    """

    def __init__(
        self,
        lrs_url: str | None = None,
        lrs_auth: str | None = None,
    ) -> None:
        self._url = (lrs_url or os.getenv("LRS_URL", "http://lrs:8080")).rstrip("/")
        self._auth = lrs_auth if lrs_auth is not None else os.getenv("LRS_AUTH", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "X-Experience-API-Version": _XAPI_VERSION_HEADER,
        }
        if self._auth:
            h["Authorization"] = f"Basic {self._auth}"
        return h

    @property
    def _statements_endpoint(self) -> str:
        return f"{self._url}/xapi/statements"

    # ------------------------------------------------------------------
    # BaseLMSBackend implementation
    # ------------------------------------------------------------------

    async def emit_statement(self, statement: dict) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._statements_endpoint,
                    json=statement,
                    headers=self._headers(),
                )
                ok = resp.status_code in (200, 204)
                if not ok:
                    logger.warning("LRS returned %s: %s", resp.status_code, resp.text[:200])
                return ok
        except Exception as exc:
            logger.warning("LRS emit_statement failed: %s", exc)
            return False

    async def emit_statements(self, statements: list[dict]) -> int:
        sent = 0
        for stmt in statements:
            if await self.emit_statement(stmt):
                sent += 1
        return sent

    def emit_statement_sync(self, statement: dict, timeout: float = 2.0) -> bool:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    self._statements_endpoint,
                    json=statement,
                    headers=self._headers(),
                )
                ok = resp.status_code in (200, 204)
                if not ok:
                    logger.warning("LRS sync returned %s: %s", resp.status_code, resp.text[:200])
                return ok
        except Exception as exc:
            logger.warning("LRS emit_statement_sync failed: %s", exc)
            return False

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self._url}/xapi/about",
                    headers=self._headers(),
                )
                return resp.status_code < 500
        except Exception:
            return False
