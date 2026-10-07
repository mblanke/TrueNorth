"""iCalendar output (RFC 5545), as ADR 0004 §Interfaces requires: it parses, UIDs are
stable, SEQUENCE rises on update, times are UTC."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

from app.scheduler import ics

T0 = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)


def unfold(doc: str) -> list[str]:
    """Undo RFC 5545 §3.1 folding: CRLF followed by one space continues the line."""
    assert doc.endswith("\r\n")
    assert "\n" not in doc.replace("\r\n", ""), "bare LF line ending"
    return doc.replace("\r\n ", "").split("\r\n")[:-1]


def events(doc: str) -> list[dict[str, str]]:
    out, cur = [], None
    for line in unfold(doc):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            out.append(cur)
            cur = None
        elif cur is not None:
            key, _, value = line.partition(":")
            cur[key] = value
    return out


def _event(**kw) -> ics.IcsEvent:
    fields = {
        "uid": ics.booking_uid(uuid.UUID(int=1)),
        "sequence": 0,
        "start": T0,
        "end": T0 + timedelta(hours=3),
        "summary": "Blue team drill",
    }
    return ics.IcsEvent(**{**fields, **kw})


def test_the_document_is_well_formed():
    doc = ics.calendar([_event()], now=T0)
    lines = unfold(doc)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines and "METHOD:PUBLISH" in lines
    assert any(line.startswith("PRODID:") for line in lines)
    (ev,) = events(doc)
    for required in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "SEQUENCE", "STATUS"):
        assert required in ev


def test_times_are_utc_whatever_zone_they_came_in():
    est = timezone(timedelta(hours=-5))
    (ev,) = events(ics.calendar([_event(start=T0.astimezone(est), end=T0.replace(tzinfo=None))], now=T0))
    assert ev["DTSTART"] == "20261015T130000Z"
    assert ev["DTEND"] == "20261015T130000Z"  # naive is taken as UTC


def test_uid_is_stable_and_sequence_comes_from_the_booking():
    booking = uuid.uuid4()
    first = events(ics.calendar([_event(uid=ics.booking_uid(booking), sequence=0)], now=T0))[0]
    moved = events(
        ics.calendar([_event(uid=ics.booking_uid(booking), sequence=1, start=T0 + timedelta(days=1))], now=T0)
    )[0]
    assert first["UID"] == moved["UID"] == f"{booking}@{ics.UID_DOMAIN}"
    assert int(moved["SEQUENCE"]) > int(first["SEQUENCE"])


def test_text_is_escaped():
    (ev,) = events(ics.calendar([_event(summary="Red; blue, and\\green", description="line one\nline two")], now=T0))
    assert ev["SUMMARY"] == "Red\\; blue\\, and\\\\green"
    assert ev["DESCRIPTION"] == "line one\\nline two"


def test_long_lines_fold_at_75_octets_without_splitting_characters():
    doc = ics.calendar([_event(summary="Exercice de défense " * 12)], now=T0)
    for physical in doc.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75
    assert events(doc)[0]["SUMMARY"] == ics.escape_text("Exercice de défense " * 12)


def test_a_cancelled_event_says_so():
    (ev,) = events(ics.calendar([_event(status="CANCELLED")], now=T0))
    assert ev["STATUS"] == "CANCELLED"
