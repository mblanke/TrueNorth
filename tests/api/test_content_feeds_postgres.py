"""The content feeds on PostgreSQL (ADR 0014 decision 25, issue #287): the migrated schema
(``postgres_engine`` is ``alembic upgrade head``), the worker's pull storing entries through
the table mirror, and the content clock's sync turning them into a flag, once.

Runs only with TEST_POSTGRES_ADMIN_URL (CI's test-python job).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.content_feeds import ContentFeedEntry, ContentFeedFlag, ContentFeedSource
from app.content_review import ContentReviewItem
from app.models import GoldenImage
from sqlalchemy.orm import Session
from worker import content_review_sync as sync_mod
from worker.content_feeds import catalogue, pull

pytestmark = pytest.mark.slow

FIX = Path(__file__).resolve().parents[1] / "worker/content_feeds_fixtures"
PRODUCTS = catalogue.load(FIX / "products.yaml")
TRACKED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 27, 4, 10, tzinfo=UTC)


class FakeHttp:
    def get_json(self, url, *, params=None, headers=None):
        if url.endswith("/ubuntu.json"):
            return json.loads((FIX / "endoflife_ubuntu.json").read_text())
        return []


def test_pull_then_sync_flags_the_golden_template_once(postgres_engine, monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "online")
    eng = postgres_engine
    gid = uuid.uuid4()
    with Session(eng) as db:
        db.add(GoldenImage(id=gid, catalogue_id="ubuntu-old", hypervisor="vsphere", os_aliases="[]"))
        db.commit()
    with Session(eng) as db:
        sync_mod.sync(db, now=TRACKED, feed_products=PRODUCTS)
        db.commit()

    @contextmanager
    def session():
        with Session(eng) as db:
            yield db
            db.commit()

    out = pull.pull(now=NOW, http=FakeHttp(), sources=("endoflife",), products=PRODUCTS, session=session,
                    after=lambda: None)
    assert out["sources"]["endoflife"]["status"] == "ok"
    for _ in range(2):  # the second run raises nothing new
        with Session(eng) as db:
            sync_mod.sync(db, now=NOW, feed_products=PRODUCTS)
            db.commit()
    with Session(eng) as db:
        assert db.query(ContentFeedEntry).filter_by(entry_key="eol:ubuntu:20.04").one().origin == "online"
        assert db.get(ContentFeedSource, "endoflife").last_success_at is not None
        row = db.query(ContentReviewItem).filter_by(item_type="golden_template", item_id=str(gid)).one()
        assert [r["text"] for r in row.stale_reasons] == ["Ubuntu 20.04 reached EOL 2025-05-31"]
        assert row.stale is True
        assert db.query(ContentFeedFlag).filter_by(item_id=row.id).count() == 1
