"""iCalendar (RFC 5545) output for bookings (ADR 0004 §6).

Written by hand rather than with a library: the subset needed (one VCALENDAR of
VEVENTs, UTC times, text escaping, line folding) is small and fully specified, and a
new dependency would be one more supply-chain entry for it.

- ``METHOD:PUBLISH`` for the subscription feed; slice 6 uses ``REQUEST``/``CANCEL``.
- ``UID`` is stable per booking, so clients update rather than duplicate an event.
- ``SEQUENCE`` comes from the booking and rises when it changes.
- Times are UTC with a ``Z`` suffix; the client shows them in its own time zone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

PRODID = "-//TrueNorth Range//Scheduler//EN"
UID_DOMAIN = "scheduler.truenorth-range"
CRLF = "\r\n"


@dataclass(frozen=True)
class IcsEvent:
    uid: str
    sequence: int
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    status: str = "CONFIRMED"  # CONFIRMED | TENTATIVE | CANCELLED
    last_modified: datetime | None = None
    organizer: str = ""  # email; required with METHOD:REQUEST/CANCEL
    attendees: tuple[str, ...] = ()  # emails


def booking_uid(booking_id: object) -> str:
    return f"{booking_id}@{UID_DOMAIN}"


def utc(dt: datetime) -> str:
    """``20261015T130000Z``. A naive datetime is taken to be UTC (how SQLite returns them)."""
    dt = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def escape_text(value: str) -> str:
    """TEXT escaping (RFC 5545 §3.3.11): backslash, semicolon, comma, newline."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1), never inside a UTF-8 character."""
    out: list[str] = []
    current = b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > 75:
            out.append(current.decode("utf-8"))
            current = b" "  # a continuation line starts with one space, which counts
        current += b
    out.append(current.decode("utf-8"))
    return CRLF.join(out)


def calendar(
    events: Iterable[IcsEvent], *, method: str = "PUBLISH", name: str = "TrueNorth Range", now: datetime | None = None
) -> str:
    """A complete VCALENDAR document, CRLF-terminated lines."""
    stamp = utc(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        f"X-WR-CALNAME:{escape_text(name)}",
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{utc(e.start)}",
            f"DTEND:{utc(e.end)}",
            f"SEQUENCE:{e.sequence}",
            f"STATUS:{e.status}",
            f"SUMMARY:{escape_text(e.summary)}",
        ]
        if e.description:
            lines.append(f"DESCRIPTION:{escape_text(e.description)}")
        if e.last_modified:
            lines.append(f"LAST-MODIFIED:{utc(e.last_modified)}")
        if e.organizer:
            lines.append(f"ORGANIZER;CN=TrueNorth Range:mailto:{e.organizer}")
        for who in e.attendees:
            lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{who}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return CRLF.join(fold(line) for line in lines) + CRLF
