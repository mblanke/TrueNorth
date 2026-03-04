"""Abstract base class for notification channels."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class NotificationChannel(ABC):
    """Base class that all notification channels must implement."""

    @abstractmethod
    async def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: dict | None = None,
    ) -> bool:
        """Send a single notification. Returns True on success."""
        ...

    async def send_batch(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        metadata: dict | None = None,
    ) -> dict[str, bool]:
        """Send to multiple recipients. Returns {recipient: success} map."""
        results = await asyncio.gather(
            *(self.send(r, subject, body, metadata) for r in recipients),
            return_exceptions=True,
        )
        return {
            r: (isinstance(res, bool) and res)
            for r, res in zip(recipients, results)
        }