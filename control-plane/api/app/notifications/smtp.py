"""SMTP notification channel using aiosmtplib."""
from __future__ import annotations

import logging
import os
from email.message import EmailMessage

from .base import NotificationChannel

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3


class SMTPChannel(NotificationChannel):
    """Sends email notifications via SMTP (async)."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.from_addr = os.getenv("SMTP_FROM", self.user)
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
        self._configured = bool(self.host)

    # ------------------------------------------------------------------
    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict | None = None,
    ) -> bool:
        if not self._configured:
            logger.warning("SMTP not configured — logging notification instead")
            logger.info("Notification to=%s subj=%s body=%s", recipient, subject, body[:200])
            return False

        import aiosmtplib  # deferred import so module loads even w/o dep

        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)

        if metadata and metadata.get("html"):
            msg.add_alternative(metadata["html"], subtype="html")

        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                await aiosmtplib.send(
                    msg,
                    hostname=self.host,
                    port=self.port,
                    username=self.user or None,
                    password=self.password or None,
                    start_tls=self.use_tls,
                )
                logger.info("Email sent to %s (attempt %d)", recipient, attempt)
                return True
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "SMTP attempt %d/%d failed for %s: %s",
                    attempt,
                    _MAX_RETRIES,
                    recipient,
                    exc,
                )
        logger.error("SMTP send failed after %d attempts: %s", _MAX_RETRIES, last_err)
        return False

    # ------------------------------------------------------------------
    async def test_connection(self) -> bool:
        """Verify SMTP credentials without sending an email."""
        if not self._configured:
            return False
        import aiosmtplib

        try:
            smtp = aiosmtplib.SMTP(hostname=self.host, port=self.port)
            await smtp.connect()
            if self.use_tls:
                await smtp.starttls()
            if self.user:
                await smtp.login(self.user, self.password)
            await smtp.quit()
            return True
        except Exception as exc:
            logger.error("SMTP connection test failed: %s", exc)
            return False