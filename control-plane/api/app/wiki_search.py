"""Wiki search through the search backend (OpenSearch), with the database as the authority (#380).

OpenSearch is used to **rank** pages (stemming, phrases, title and tag boosts); it is never
asked who may see one. Its answer is a list of page ids, filtered by tenant. The router
loads those ids from the database with every visibility rule it already applies (tenant,
deleted, archived, published, staff-only, system-owned), so a stale or wrong index can hide
a page from the ranking but can never show anyone a page they may not read. The index
carries no visibility fields at all.

Results the old substring search finds are always kept (behind the ranked ones), so search
is never worse than before; and with no OpenSearch, a down one, or a slow one (3 s) it is
exactly the old search.

Indexing follows the database: any flush that touches a ``WikiPage`` records its document;
when that session **commits** the documents are sent (a rolled-back change sends nothing),
off the request, never failing it. A page that is soft-deleted is removed from the index.
``POST /wiki/search/reindex`` (WIKI_ADMIN) fills the index for pages that predate this.

On by default only when ``OPENSEARCH_URL`` is set and ``SEARCH_BACKEND`` is not ``null``;
``WIKI_OPENSEARCH=false`` turns it off, ``true`` forces it on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from .models_wiki import WikiPage
from .search_backends import get_search_backend
from .search_backends.base import BaseSearchBackend
from .search_backends.connection import url_configured

logger = logging.getLogger("truenorth.api.wiki_search")

INDEX = "truenorth-wiki"
PENDING_KEY = "tn_wiki_search_pending"
QUERY_TIMEOUT_S = 3.0
SIMPLE_QUERY_FLAGS = "AND|OR|NOT|PHRASE|PREFIX|PRECEDENCE|WHITESPACE|ESCAPE"  # no FUZZY / NEAR / SLOP

INDEX_BODY = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {
        "dynamic": False,
        "properties": {
            "tenant_id": {"type": "keyword"},
            "title": {"type": "text", "analyzer": "english"},
            "tags": {"type": "text", "analyzer": "english"},
            "body": {"type": "text", "analyzer": "english"},
        },
    },
}

_tasks: set[asyncio.Task] = set()


def enabled() -> bool:
    flag = os.getenv("WIKI_OPENSEARCH", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return url_configured() and os.getenv("SEARCH_BACKEND", "opensearch").strip().lower() != "null"


def _backend() -> BaseSearchBackend | None:
    try:
        backend = get_search_backend()
    except Exception:  # noqa: BLE001 — an unknown SEARCH_BACKEND must not break wiki search
        return None
    return backend if backend.supports_documents else None


def document(page: WikiPage) -> dict[str, str]:
    return {
        "tenant_id": str(page.tenant_id),
        "title": page.title or "",
        "tags": page.tags or "",
        "body": page.body or "",
    }


def build_query(q: str, tenant_id: uuid.UUID) -> dict:
    """Free text only, scoped to one tenant. Never the store's own ``query_string`` parser."""
    return {
        "bool": {
            "filter": [{"term": {"tenant_id": str(tenant_id)}}],
            "must": [
                {
                    "simple_query_string": {
                        "query": q,
                        "fields": ["title^4", "tags^3", "body"],
                        "flags": SIMPLE_QUERY_FLAGS,
                        "default_operator": "and",
                        "lenient": True,
                    }
                }
            ],
        }
    }


async def _ranked(q: str, tenant_id: uuid.UUID, size: int) -> list[uuid.UUID] | None:
    backend = _backend()
    if backend is None:
        return None
    found = await asyncio.wait_for(backend.match(INDEX, build_query(q, tenant_id), size=size), QUERY_TIMEOUT_S)
    out: list[uuid.UUID] = []
    for raw in found.ids:
        try:
            out.append(uuid.UUID(raw))
        except ValueError:
            continue  # not one of ours
    return out


def ranked_ids(q: str, tenant_id: uuid.UUID, size: int) -> list[uuid.UUID] | None:
    """Page ids best-first for `q`, or None when the search backend cannot answer.

    Called from a route running on an AnyIO worker thread."""
    if not enabled():
        return None
    try:
        from anyio import from_thread

        return from_thread.run(_ranked, q, tenant_id, size)
    except Exception as exc:  # noqa: BLE001 — never let the index break search
        logger.warning("Wiki search backend unavailable, using the database: %s", exc)
        return None


# ── Keeping the index in step with the database ─────────────────────────
async def apply(ops: dict[uuid.UUID, dict | None]) -> None:
    """`ops` maps page id to its document, or None to remove it from the index."""
    backend = _backend()
    if backend is None or not ops:
        return
    docs = {str(i): d for i, d in ops.items() if d is not None}
    gone = [str(i) for i, d in ops.items() if d is None]
    if docs:
        await backend.upsert_documents(INDEX, docs, settings=INDEX_BODY)
    if gone:
        await backend.delete_documents(INDEX, gone)


async def _apply_logged(ops: dict[uuid.UUID, dict | None]) -> None:
    try:
        await asyncio.wait_for(apply(ops), 10.0)
    except Exception as exc:  # noqa: BLE001 — the index is a convenience; search falls back
        logger.warning("Could not update the wiki search index: %s", exc)


def _spawn(ops: dict[uuid.UUID, dict | None]) -> None:
    def start() -> None:
        task = asyncio.get_running_loop().create_task(_apply_logged(ops))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        start()
        return
    try:
        from anyio import from_thread

        from_thread.run_sync(start)
    except Exception:  # noqa: BLE001 — a script or a Celery task: no event loop; reindex catches up
        logger.debug("No event loop to update the wiki search index on")


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _ctx: Any) -> None:
    if not enabled():
        return
    for obj in (*session.new, *session.dirty):
        if isinstance(obj, WikiPage) and obj.id is not None:
            gone = obj.deleted_at is not None
            session.info.setdefault(PENDING_KEY, {})[obj.id] = (obj, None if gone else document(obj))


@event.listens_for(Session, "after_soft_rollback")
def _forget(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:  # the whole transaction, not a savepoint inside it
        session.info.pop(PENDING_KEY, None)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(PENDING_KEY, None)
    if not pending:
        return
    ops: dict[uuid.UUID, dict | None] = {}
    for page_id, (obj, doc) in pending.items():
        try:
            if inspect(obj).persistent:  # a savepoint that rolled back leaves it transient
                ops[page_id] = doc
        except Exception:  # noqa: BLE001
            continue
    if ops:
        try:
            _spawn(ops)
        except Exception:  # noqa: BLE001 — never turn a successful commit into an error
            logger.warning("Wiki search indexing failed", exc_info=True)


def reindex(db: Session, tenant_id: uuid.UUID, batch: int = 200) -> int:
    """Index every live page of a tenant (and drop soft-deleted ones). Raises if the store cannot take them.

    Called from a route on an AnyIO worker thread."""
    from anyio import from_thread

    if _backend() is None:
        raise RuntimeError("no search backend that stores documents is configured")
    total = 0
    q = db.query(WikiPage).filter(WikiPage.tenant_id == tenant_id).order_by(WikiPage.id)
    start = 0
    while True:
        pages = q.offset(start).limit(batch).all()
        if not pages:
            return total
        ops = {p.id: (None if p.deleted_at else document(p)) for p in pages}
        from_thread.run(apply, ops)
        total += sum(1 for d in ops.values() if d is not None)
        start += batch
