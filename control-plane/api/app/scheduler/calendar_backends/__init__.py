"""TrueNorth Range — external calendar sync registry (ADR 0004 §7).

Usage:
    from app.scheduler.calendar_backends import get_calendar_backend

    await get_calendar_backend().publish(event)

Configuration:
    CALENDAR_BACKEND selects the backend (default: null).
        null  — no external sync; the feed and emailed invites only

Adding a backend (planned: microsoft_graph): implement BaseCalendarBackend in
calendar_backends/<name>.py and add it to _REGISTRY. The contract suite
(tests/contracts/test_adapter_contracts.py, seam ``calendar``) enrols it automatically.
"""

from __future__ import annotations

import logging
import os

from ..ics import IcsEvent
from .base import BaseCalendarBackend
from .null import NullCalendarBackend

__all__ = ["BaseCalendarBackend", "NullCalendarBackend", "get_calendar_backend", "push"]

logger = logging.getLogger("truenorth.scheduler.calendar")

_REGISTRY: dict[str, type[BaseCalendarBackend]] = {
    "null": NullCalendarBackend,
}

_instance: BaseCalendarBackend | None = None


def get_calendar_backend() -> BaseCalendarBackend:
    """Singleton backend for CALENDAR_BACKEND. Raises ValueError for unknown names."""
    global _instance
    if _instance is None:
        name = os.getenv("CALENDAR_BACKEND", "null").lower()
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown CALENDAR_BACKEND {name!r}; expected one of {sorted(_REGISTRY)}")
        _instance = cls()
    return _instance


def reset_calendar_backend() -> None:
    """Drop the singleton (tests, or after changing CALENDAR_BACKEND)."""
    global _instance
    _instance = None


async def push(event: IcsEvent, cancelled: bool) -> None:
    """Best effort, after the response: a sync failure is logged, the booking stands."""
    try:
        backend = get_calendar_backend()
        await (backend.cancel(event) if cancelled else backend.publish(event))
    except Exception:
        logger.exception("Calendar sync failed for %s", event.uid)
