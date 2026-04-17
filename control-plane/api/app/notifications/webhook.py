"""Webhook notification channel (async HTTP POST with HMAC signing)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from .base import NotificationChannel

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class WebhookChannel(NotificationChannel):
    """POST JSON payloads to a configurable webhook endpoint."""

    def __init__(self) -> None:
        self.url = os.getenv("WEBHOOK_URL", "")
        self.secret = os.getenv("WEBHOOK_SECRET", "")
        self.timeout = float(os.getenv("WEBHOOK_TIMEOUT", "10"))
        self._configured = bool(self.url)

    # ------------------------------------------------------------------
    def _sign(self, payload_bytes: bytes) -> str | None:
        """Return HMAC-SHA256 hex signature if a secret is configured."""
        if not self.secret:
            return None
        return hmac.new(
            self.secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict | None = None,
    ) -> bool:
        if not self._configured:
            logger.warning("Webhook URL not configured — skipping notification")
            return False

        payload = {
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "metadata": metadata or {},
            "timestamp": time.time(),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        sig = self._sign(raw)
        if sig:
            headers["X-Signature-256"] = f"sha256={sig}"

        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(self.url, content=raw, headers=headers)
                    resp.raise_for_status()
                logger.info("Webhook delivered to %s (attempt %d)", self.url, attempt)
                return True
            except Exception as exc:
                last_err = exc
                delay = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Webhook attempt %d/%d failed: %s — retrying in %.1fs",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        logger.error("Webhook failed after %d attempts: %s", _MAX_RETRIES, last_err)
        return False
