"""SMTP notification channel using aiosmtplib.

Settings come from the environment (the installer's ``tn_smtp_*``, Helm's
``config.smtp``; install/README.md "Email (SMTP)"):

``SMTP_HOST``      the relay; empty: email is off and every message is only logged
``SMTP_PORT``      default 587
``SMTP_SECURITY``  ``starttls`` (default) | ``tls`` (implicit TLS) | ``none``. Unset, the
                   older ``SMTP_USE_TLS`` (true: starttls, false: none) still applies.
``SMTP_USER`` / ``SMTP_PASSWORD``  empty: no sign-in
``SMTP_FROM``      the sender; empty falls back to ``SMTP_USER``

``smtp_settings()`` is the one reading of them: the channel sends with it and
``GET /email/status`` (app/email_status) reports from it, so the two cannot disagree.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from email.message import EmailMessage

from .base import NotificationChannel

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
SECURITY_MODES = ("starttls", "tls", "none")


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    security: str
    user: str
    password: str
    from_addr: str

    @property
    def configured(self) -> bool:
        """Mail can be handed to a relay: a host, a sender, a usable port and mode."""
        return bool(self.host and self.from_addr and 0 < self.port < 65536 and self.security in SECURITY_MODES)


def smtp_settings() -> SMTPSettings:
    """The SMTP_* environment, read now (tests and a restarted api see changes)."""
    user = os.getenv("SMTP_USER", "").strip()
    security = os.getenv("SMTP_SECURITY", "").strip().lower()
    if not security:
        legacy = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("true", "1", "yes")
        security = "starttls" if legacy else "none"
    try:
        port = int(os.getenv("SMTP_PORT", "").strip() or "587")
    except ValueError:
        port = 0
    return SMTPSettings(
        host=os.getenv("SMTP_HOST", "").strip(),
        port=port,
        security=security,
        user=user,
        password=os.getenv("SMTP_PASSWORD", ""),
        from_addr=os.getenv("SMTP_FROM", "").strip() or user,
    )


class SMTPChannel(NotificationChannel):
    """Sends email notifications via SMTP (async)."""

    def __init__(self, attempts: int = _MAX_RETRIES, timeout: float = 60.0) -> None:
        self.attempts = max(1, attempts)
        self.timeout = timeout  # seconds per attempt (aiosmtplib connect and each command)
        cfg = smtp_settings()
        self.host = cfg.host
        self.port = cfg.port
        self.user = cfg.user
        self.password = cfg.password
        self.from_addr = cfg.from_addr
        self.security = cfg.security
        self.use_tls = cfg.security == "starttls"  # STARTTLS; kept for existing callers
        self._configured = cfg.configured

    def _tls_kwargs(self) -> dict:
        """aiosmtplib: use_tls is implicit TLS; start_tls upgrades a plain connection."""
        return {"use_tls": self.security == "tls", "start_tls": self.security == "starttls"}

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
        if metadata and metadata.get("calendar"):
            # An iCalendar invite (RFC 6047): mail clients show Accept/Decline for it.
            method, ics_text = metadata["calendar"]
            msg.add_alternative(ics_text, subtype="calendar", params={"method": method})

        last_err: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                await aiosmtplib.send(
                    msg,
                    hostname=self.host,
                    timeout=self.timeout,
                    port=self.port,
                    username=self.user or None,
                    password=self.password or None,
                    **self._tls_kwargs(),
                )
                logger.info("Email sent to %s (attempt %d)", recipient, attempt)
                return True
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "SMTP attempt %d/%d failed for %s: %s",
                    attempt,
                    self.attempts,
                    recipient,
                    exc,
                )
        logger.error("SMTP send failed after %d attempts: %s", self.attempts, last_err)
        return False

    # ------------------------------------------------------------------
    async def test_connection(self) -> bool:
        """Verify SMTP credentials without sending an email."""
        if not self._configured:
            return False
        import aiosmtplib

        try:
            smtp = aiosmtplib.SMTP(hostname=self.host, port=self.port, use_tls=self.security == "tls", start_tls=False)
            await smtp.connect()
            if self.security == "starttls":
                await smtp.starttls()
            if self.user:
                await smtp.login(self.user, self.password)
            await smtp.quit()
            return True
        except Exception as exc:
            logger.error("SMTP connection test failed: %s", exc)
            return False
