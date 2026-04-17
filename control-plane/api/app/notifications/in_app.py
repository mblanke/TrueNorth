"""In-app notification channel — stores in DB and optionally pushes via WebSocket."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from .base import NotificationChannel

logger = logging.getLogger(__name__)


class InAppChannel(NotificationChannel):
    """Persist notifications in the database and optionally push via WS."""

    def __init__(self, db_session_factory=None, ws_manager=None) -> None:
        self._db_session_factory = db_session_factory
        self._ws_manager = ws_manager

    # ------------------------------------------------------------------
    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict | None = None,
    ) -> bool:
        notification_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        # Persist to database if session factory is available
        if self._db_session_factory:
            try:
                async with self._db_session_factory() as session:
                    from sqlalchemy import text

                    await session.execute(
                        text(
                            "INSERT INTO notifications "
                            "(id, recipient, subject, body, metadata, created_at, read) "
                            "VALUES (:id, :recipient, :subject, :body, :metadata, :created_at, false)"
                        ),
                        {
                            "id": notification_id,
                            "recipient": recipient,
                            "subject": subject,
                            "body": body,
                            "metadata": str(metadata or {}),
                            "created_at": now.isoformat(),
                        },
                    )
                    await session.commit()
            except Exception as exc:
                logger.error("Failed to persist in-app notification: %s", exc)
                return False
        else:
            logger.info(
                "In-app notification (no DB): id=%s to=%s subj=%s",
                notification_id,
                recipient,
                subject,
            )

        # Push via WebSocket if manager is available
        if self._ws_manager:
            try:
                await self._ws_manager.send_to_user(
                    recipient,
                    {
                        "type": "notification",
                        "id": notification_id,
                        "subject": subject,
                        "body": body,
                        "metadata": metadata or {},
                        "created_at": now.isoformat(),
                    },
                )
            except Exception as exc:
                logger.warning("WS push failed for notification %s: %s", notification_id, exc)

        return True
