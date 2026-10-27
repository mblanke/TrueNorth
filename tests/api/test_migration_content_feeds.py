"""4f2e4c9cd1a0 adds the content feeds' three tables, and only those (ADR 0014 §25, issue #287).

Upgrade from its parent onto a database holding a review row: the tables appear with their
keys, nothing is written, and a flag on that row can be written through the ORM. Downgrade
drops all three and keeps the review row; re-upgrade restores them. SQLite here; the
PostgreSQL chain runs in CI (test_migration_populated_upgrade.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "0fdeb8975b40", "4f2e4c9cd1a0"
TABLES = {"content_feed_entries", "content_feed_flags", "content_feed_sources"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_single_head():
    text = (API / f"alembic/versions/{REVISION}_content_feeds.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'feeds.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    from app.content_review import ContentReviewItem
    from sqlalchemy.orm import Session

    item = uuid.uuid4()
    with Session(engine) as db:
        db.add(ContentReviewItem(id=item, tenant_id=None, item_type="golden_template", item_id=str(uuid.uuid4()),
                                 title="ubuntu-lts", theme_ids=[], stale_reasons=[], due_at=datetime.now(UTC)))
        db.commit()
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("content_feed_flags")} == {
        ("item_id", "entry_key")}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("content_feed_flags")} == {"content_review_items"}
    with engine.connect() as db:
        for table in TABLES:
            assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0, table

    from app.content_feeds import ContentFeedEntry, ContentFeedFlag, ContentFeedSource

    now = datetime.now(UTC)
    with Session(engine) as db:  # the ORM's models are the migration's schema
        db.add(ContentFeedEntry(id=uuid.uuid4(), entry_key="eol:ubuntu:20.04", source="endoflife", kind="release",
                                products=["eol:ubuntu:20.04"], at=now, title="", data={}, origin="online",
                                first_seen_at=now, updated_at=now))
        db.add(ContentFeedFlag(id=uuid.uuid4(), item_id=item, entry_key="eol:ubuntu:20.04", raised_at=now))
        db.add(ContentFeedSource(source="endoflife", last_error="", entries=1, cursor="", detail={}))
        db.commit()

    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM content_review_items")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
