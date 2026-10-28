"""The content clock's feeds step (run inside ``content_review_sync.sync``).

Reads the stored feed entries and the flags already raised, finds each live golden
template's products in the product map (by its catalogue id), and flags the new matches on
the sync's graph (``staleness.Graph.flag_stale``: the item's own reason, carried down the
edges). The sync writes the rows, then records the flags (``record``) only for rows it
actually wrote, so a row a review changed meanwhile is flagged again on the next run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from ..tables import content_feed_entries, content_feed_flags, content_feed_settings, golden_images
from . import catalogue, match, settings

logger = logging.getLogger("truenorth.worker.content_feeds")

CHUNK = 500


def _products(mapping):
    if mapping is not None:
        return mapping
    try:
        return catalogue.load()
    except catalogue.CatalogueError as exc:
        logger.error("[content-feeds] %s: no feed flags this run", exc)
        return {}


def _utc(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def enabled_at(db, now: datetime) -> datetime:
    """When feeds were first on (``content_feed_settings``, row 1), recorded now if never.
    With the day an item was first tracked, the later of the two anchors which
    vulnerabilities may flag it: turning feeds on never flags the history before."""
    row = db.execute(sa.select(content_feed_settings.c.enabled_at).where(content_feed_settings.c.id == 1)).first()
    if row is not None and row.enabled_at is not None:
        return _utc(row.enabled_at)
    if row is None:
        db.execute(content_feed_settings.insert().values(id=1, enabled_at=now))
    else:
        db.execute(content_feed_settings.update().where(content_feed_settings.c.id == 1).values(enabled_at=now))
    return now


def flags(db, live: dict[str, dict], now: datetime, *, products=None) -> list[match.Flag]:
    """New flags for the live (not retired) golden templates. ``live``: review row id -> row.
    None once feeds are turned off: stored end-of-life dates still to come raise nothing."""
    if settings.mode() == "off":
        return []
    enabled = enabled_at(db, now)
    golden = {str(r["item_id"]): rid for rid, r in live.items()
              if r["item_type"] == "golden_template" and not r["retired"]}
    if not golden or db.execute(sa.select(content_feed_entries.c.id).limit(1)).first() is None:
        return []
    mapping = _products(products)
    if not mapping:
        return []
    tracked: dict[str, match.Tracked] = {}
    ids = sorted(golden)
    for i in range(0, len(ids), CHUNK):
        for g in db.execute(sa.select(golden_images.c.id, golden_images.c.catalogue_id)
                            .where(golden_images.c.id.in_(ids[i:i + CHUNK]))).all():
            rid = golden.get(str(g.id))
            found = mapping.get(g.catalogue_id)
            if rid and found:
                first = _utc(live[rid]["created_at"]) or now
                tracked[rid] = match.Tracked(products=tuple(found), since=max(first, enabled))
    if not tracked:
        return []
    raised = set()
    rids = sorted(tracked)
    for i in range(0, len(rids), CHUNK):
        raised |= {(str(f.item_id), f.entry_key) for f in db.execute(
            sa.select(content_feed_flags.c.item_id, content_feed_flags.c.entry_key)
            .where(content_feed_flags.c.item_id.in_(rids[i:i + CHUNK]))).all()}
    entries = [r._asdict() for r in db.execute(sa.select(
        content_feed_entries.c.entry_key.label("key"), content_feed_entries.c.kind, content_feed_entries.c.products,
        content_feed_entries.c.at, content_feed_entries.c.title, content_feed_entries.c.data)).all()]
    return match.flags_for(tracked, entries, raised, now)


def record(db, raised: Iterable[match.Flag], now: datetime) -> int:
    rows = [{"id": str(uuid.uuid4()), "item_id": f.item, "entry_key": f.entry_key, "raised_at": now} for f in raised]
    if rows:
        db.execute(content_feed_flags.insert(), rows)
    return len(rows)
