"""No external calendar: the feed and emailed invites are the only delivery paths.

The default (CALENDAR_BACKEND=null), and the right choice on air-gapped networks.
"""

from __future__ import annotations

import logging

from ..ics import IcsEvent
from .base import BaseCalendarBackend

logger = logging.getLogger("truenorth.scheduler.calendar.null")


class NullCalendarBackend(BaseCalendarBackend):
    async def publish(self, event: IcsEvent) -> str | None:
        logger.debug("NullCalendarBackend: publish %s (sequence %d) not synced", event.uid, event.sequence)
        return None

    async def cancel(self, event: IcsEvent) -> None:
        logger.debug("NullCalendarBackend: cancel %s not synced", event.uid)

    async def health_check(self) -> bool:
        return True
