"""The daily online pull of the content feeds (ADR 0014 decision 25, issue #287).

A beat task on worker-provision, the worker with egress (``celery_app.py``). Only when
``TN_FEEDS_MODE=online``; otherwise it does nothing. For each source in
``feed_backends.ONLINE_SOURCES``: read its cursor, fetch (no database transaction is open
while the network is used), then store its entries and its status in one transaction. A
source that fails is recorded on the status panel and the rest still run. Then the content
clock's sync is queued, which turns new entries into stale flags.

What leaves the site: GET requests for the product map's public product names, through
``TN_FEEDS_HTTP_PROXY`` when set. Nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ..celery_app import app
from ..feed_backends import ONLINE_SOURCES, FeedError, FeedHttp, get_feed_source
from ..task_plumbing import db_session
from . import catalogue, settings, store

logger = logging.getLogger("truenorth.worker.content_feeds")

TASK = "worker.content_feeds.pull_content_feeds"


@app.task(bind=True, name=TASK)
def pull_content_feeds(self):
    if settings.mode() != "online":
        return {"status": "skipped", "reason": f"TN_FEEDS_MODE is {settings.mode()}, not online"}
    return pull()


def _since(status: dict | None) -> datetime | None:
    raw = (status or {}).get("cursor") or ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def queue_sync() -> None:
    from ..content_review_sync import sync_content_review

    sync_content_review.apply_async()


def pull(*, now: datetime | None = None, http: FeedHttp | None = None, sources: Iterable[str] = ONLINE_SOURCES,
         products=None, session: Callable = db_session, after: Callable[[], None] = queue_sync) -> dict:
    now = now or datetime.now(UTC)
    summary: dict = {"status": "ok", "sources": {}}
    try:
        mapping = products if products is not None else catalogue.load()
    except catalogue.CatalogueError as exc:
        logger.error("[content-feeds] %s", exc)
        with session() as db:
            for name in sources:
                store.write_status(db, name, now=now, ok=False, error=str(exc))
            db.commit()
        return {"status": "error", "error": str(exc)}
    wanted = catalogue.all_products(mapping) if isinstance(mapping, dict) else list(mapping)
    own_http = http is None
    client = http or FeedHttp(proxy=settings.proxy())
    stored = 0
    try:
        for name in sources:
            source = get_feed_source(name)
            with session() as db:
                since = _since(store.read_status(db, name))
            try:
                result = source.fetch(client, wanted, since=since, now=now)
            except FeedError as exc:
                logger.warning("[content-feeds] %s: %s", name, exc)
                with session() as db:
                    store.write_status(db, name, now=now, ok=False, error=str(exc))
                    db.commit()
                summary["sources"][name] = {"status": "error", "error": str(exc)}
                summary["status"] = "partial"
                continue
            try:
                with session() as db:
                    counts = store.upsert_entries(db, result.entries, origin="online", now=now)
                    store.write_status(db, name, now=now, ok=True, error="; ".join(result.errors),
                                       entries_count=len(result.entries), cursor=result.cursor)
                    db.commit()
            except Exception as exc:  # noqa: BLE001 -- e.g. two pulls racing on an entry: this source only
                error = f"its entries could not be stored ({type(exc).__name__}); the next pull retries"
                logger.warning("[content-feeds] %s: %s", name, error)
                with session() as db:
                    store.write_status(db, name, now=now, ok=False, error=error)
                    db.commit()
                summary["sources"][name] = {"status": "error", "error": error}
                summary["status"] = "partial"
                continue
            stored += counts["new"] + counts["updated"]
            summary["sources"][name] = {"status": "ok", **counts, "errors": len(result.errors)}
    finally:
        if own_http:
            client.close()
    if stored:
        after()
    logger.info("[content-feeds] %s", summary)
    return summary
