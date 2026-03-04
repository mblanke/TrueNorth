"""TrueNorth Range - Multi-channel notification system.

Supports WebSocket push, email (SMTP), outbound webhooks, and persistent
in-app notifications with read/unread tracking.
"""

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Any
from uuid import uuid4

logger = logging.getLogger("truenorth.notifications")


class NotificationChannel(str, Enum):
    """Delivery channels for a notification."""

    WEBSOCKET = "websocket"
    EMAIL = "email"
    WEBHOOK = "webhook"
    IN_APP = "in_app"


class NotificationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


@dataclass
class Notification:
    """A routable notification destined for one or more channels."""

    title: str
    message: str
    level: str  # info, warning, error, success
    channels: list[NotificationChannel]
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    recipient_user_id: str | None = None
    recipient_tenant_id: str | None = None
    recipient_role: str | None = None  # Send to all users with this role
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["channels"] = [c.value if isinstance(c, NotificationChannel) else c for c in self.channels]
        return d


class NotificationService:
    """Route notifications to the appropriate delivery channel(s).

    Parameters
    ----------
    redis_url:
        Optional Redis URL used for WebSocket pub/sub fan-out.
    smtp_config:
        Dict with keys ``host``, ``port``, ``username``, ``password``,
        ``from_addr``.  When *None* email delivery is a no-op.
    webhook_urls:
        Default webhook endpoints to POST to.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        smtp_config: dict | None = None,
        webhook_urls: list[str] | None = None,
    ):
        self._redis_url = redis_url
        self._smtp_config = smtp_config or {}
        self._webhook_urls = webhook_urls or []
        # In-memory store (swap for DB/Redis in production)
        self._in_app_store: dict[str, list[dict]] = {}  # user_id -> [notification]
        self._sent_log: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, notification: Notification) -> dict:
        """Route *notification* to all requested channels.

        Returns a dict mapping each channel to its delivery status.
        """
        results: dict[str, str] = {}
        for channel in notification.channels:
            try:
                if channel == NotificationChannel.WEBSOCKET:
                    await self._send_websocket(notification)
                elif channel == NotificationChannel.EMAIL:
                    await self._send_email(notification)
                elif channel == NotificationChannel.WEBHOOK:
                    await self._send_webhook(notification)
                elif channel == NotificationChannel.IN_APP:
                    await self._store_in_app(notification)
                results[channel.value] = "sent"
            except Exception as exc:
                logger.exception("Failed %s delivery for %s", channel.value, notification.id)
                results[channel.value] = f"error: {exc}"
        self._sent_log.append(
            {"id": notification.id, "channels": results, "ts": notification.created_at}
        )
        return results

    # ------------------------------------------------------------------
    # Channel implementations
    # ------------------------------------------------------------------

    async def _send_websocket(self, notification: Notification) -> None:
        """Push notification payload through WebSocket (or Redis pub/sub)."""
        payload = notification.to_dict()
        logger.info(
            "WS notification: [%s] %s -> user=%s tenant=%s",
            notification.level,
            notification.title,
            notification.recipient_user_id,
            notification.recipient_tenant_id,
        )
        # In production: websocket_manager.send_to_user(...)

    async def _send_email(self, notification: Notification) -> None:
        """Send notification via SMTP."""
        if not self._smtp_config:
            logger.warning("SMTP not configured; skipping email for %s", notification.id)
            return
        logger.info(
            "EMAIL notification: [%s] %s -> user=%s",
            notification.level,
            notification.title,
            notification.recipient_user_id,
        )
        # In production: aiosmtplib send

    async def _send_webhook(self, notification: Notification) -> None:
        """POST notification JSON to configured webhook URLs."""
        payload = notification.to_dict()
        targets = list(self._webhook_urls)
        if notification.data.get("webhook_url"):
            targets.append(notification.data["webhook_url"])
        for url in targets:
            logger.info("WEBHOOK -> %s : %s", url, notification.title)
            # In production: httpx.AsyncClient().post(url, json=payload)

    async def _store_in_app(self, notification: Notification) -> None:
        """Persist notification for later retrieval by the user."""
        user_id = notification.recipient_user_id
        if not user_id:
            logger.warning("in_app notification has no recipient_user_id; skipping")
            return
        entry = notification.to_dict()
        entry["read"] = False
        self._in_app_store.setdefault(user_id, []).insert(0, entry)
        logger.info("IN_APP stored for user %s", user_id)

    # ------------------------------------------------------------------
    # In-app query helpers
    # ------------------------------------------------------------------

    async def get_user_notifications(
        self, user_id: str, limit: int = 50
    ) -> list[dict]:
        """Return most recent *limit* in-app notifications for *user_id*."""
        items = self._in_app_store.get(user_id, [])
        return items[:limit]

    async def mark_read(self, notification_id: str, user_id: str) -> bool:
        """Mark a single notification as read.  Returns *True* if found."""
        for item in self._in_app_store.get(user_id, []):
            if item["id"] == notification_id:
                item["read"] = True
                return True
        return False

    async def mark_all_read(self, user_id: str) -> int:
        """Mark every notification as read for *user_id*.  Returns count."""
        count = 0
        for item in self._in_app_store.get(user_id, []):
            if not item["read"]:
                item["read"] = True
                count += 1
        return count

    async def get_unread_count(self, user_id: str) -> int:
        """Return the number of unread in-app notifications."""
        return sum(
            1
            for item in self._in_app_store.get(user_id, [])
            if not item["read"]
        )

    async def delete_notification(self, notification_id: str, user_id: str) -> bool:
        """Delete a notification.  Returns *True* if found and removed."""
        items = self._in_app_store.get(user_id, [])
        for i, item in enumerate(items):
            if item["id"] == notification_id:
                items.pop(i)
                return True
        return False