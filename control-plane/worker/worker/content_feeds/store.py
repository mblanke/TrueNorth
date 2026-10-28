"""Storing feed entries and each source's status (SQLAlchemy Core on the generated mirror,
tables.py: no raw SQL in the worker, ADR 0003). The API's bundle import writes the same
rows through its ORM (``app/content_feeds/service.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from ..feed_backends.base import FeedEntry
from ..tables import content_feed_entries as entries
from ..tables import content_feed_sources as sources

CHUNK = 500
MAX_ERROR = 1000


def _utc(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def upsert_entries(db, found: Iterable[FeedEntry], *, origin: str, now: datetime) -> dict:
    """Insert new entries, update changed ones (a CVE seen for another product keeps both).
    Returns {new, updated}."""
    wanted = {e.key: e for e in found}
    existing: dict[str, dict] = {}
    keys = sorted(wanted)
    for i in range(0, len(keys), CHUNK):
        for r in db.execute(sa.select(entries.c.id, entries.c.entry_key, entries.c.products, entries.c.at,
                                      entries.c.title, entries.c.data)
                            .where(entries.c.entry_key.in_(keys[i:i + CHUNK]))).all():
            existing[r.entry_key] = r._asdict()
    new_rows, updated = [], 0
    for key, e in wanted.items():
        row = existing.get(key)
        if row is None:
            new_rows.append({"id": str(uuid.uuid4()), "entry_key": key, "source": e.source, "kind": e.kind,
                             "products": sorted(e.products), "at": e.at, "title": e.title[:500], "data": e.data,
                             "origin": origin, "first_seen_at": now, "updated_at": now})
            continue
        products = sorted({*(row["products"] or []), *e.products})
        if (products != sorted(row["products"] or []) or _utc(row["at"]) != _utc(e.at)
                or row["title"] != e.title[:500] or (row["data"] or {}) != e.data):
            db.execute(entries.update().where(entries.c.id == row["id"]).values(
                products=products, at=e.at, title=e.title[:500], data=e.data, updated_at=now))
            updated += 1
    if new_rows:
        db.execute(entries.insert(), new_rows)
    return {"new": len(new_rows), "updated": updated}


def read_status(db, source: str) -> dict | None:
    row = db.execute(sa.select(sources).where(sources.c.source == source)).first()
    return row._asdict() if row else None


def write_status(db, source: str, *, now: datetime, ok: bool, error: str = "", entries_count: int = 0,
                 cursor: str | None = None, detail: dict | None = None) -> None:
    """Record one pull of ``source``: when, whether it worked, its error (shown to admins:
    the feed sources' own messages, which never carry a key or a query)."""
    values = {"last_attempt_at": now, "last_error": error[:MAX_ERROR], "entries": entries_count}
    if ok:
        values["last_success_at"] = now
    if cursor is not None:
        values["cursor"] = cursor[:64]
    if detail is not None:
        values["detail"] = detail
    if read_status(db, source) is None:
        db.execute(sources.insert().values(**{"source": source, "last_success_at": None, "cursor": "",
                                              "detail": {}, **values}))
    else:
        db.execute(sources.update().where(sources.c.source == source).values(**values))
