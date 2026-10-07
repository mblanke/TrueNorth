"""External calendar sync interface (ADR 0004 §7; adapter registry, ADR 0001).

The feed and emailed invites need nothing on the calendar side. A backend here is for
two-way sync with a calendar system (planned: ``microsoft_graph``, for instant updates
and rooms; it needs an Entra app registration in the tenant). It receives the same
neutral event the ICS writer does, so it never reads scheduler tables.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..ics import IcsEvent


class BaseCalendarBackend(ABC):
    @abstractmethod
    async def publish(self, event: IcsEvent) -> str | None:
        """Create or update the event (matched by ``event.uid``). Returns the external
        system's id for it, or None when there is none."""

    @abstractmethod
    async def cancel(self, event: IcsEvent) -> None:
        """Cancel the event (matched by ``event.uid``); unknown is not an error."""

    @abstractmethod
    async def health_check(self) -> bool:
        """True when the external calendar can be reached (always, for ``null``)."""
