"""c1465c4964a6 adds the content clock's four tables, and only those (ADR 0014, issue #286).

Upgrade from its parent onto a database that already holds a tenant and a course: the
tables appear with their keys and indexes, the eight platform-wide themes are there, the
course is untouched, and a review row can be written through the ORM. Downgrade drops all
four; re-upgrade restores them. SQLite here; the PostgreSQL chain runs in CI
(test_migration_populated_upgrade.py with TEST_POSTGRES_ADMIN_URL).
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
PARENT, REVISION = "b62353ae80af", "c1465c4964a6"
TABLES = {"content_review_items", "content_review_edges", "content_review_themes", "content_review_cycles"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_single_head():
    text = (API / "alembic/versions/c1465c4964a6_content_review.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_tables_and_themes_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'content.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    tenant, course = uuid.uuid4(), uuid.uuid4()
    from app.models import Course, Tenant
    from sqlalchemy.orm import Session

    with Session(engine) as db:  # the parent revision's schema is the ORM's, less the new tables
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.commit()
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    cols = {c["name"] for c in insp.get_columns("content_review_items")}
    assert cols == {"id", "tenant_id", "item_type", "item_id", "title", "owner_id", "reviewer_id", "theme_ids",
                    "cycle_days", "last_reviewed_at", "last_reviewed_by", "due_at", "stale", "stale_reasons",
                    "retired", "retired_at", "retired_by", "missing", "revision", "created_at", "updated_at"}
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("content_review_items")} == {
        ("item_type", "item_id")}
    assert {i["name"] for i in insp.get_indexes("content_review_items")} >= {
        "ix_content_review_items_tenant_due", "ix_content_review_items_stale"}
    edge_fks = {fk["referred_table"] for fk in insp.get_foreign_keys("content_review_edges")}
    assert edge_fks == {"content_review_items"}
    with engine.connect() as db:
        themes = db.execute(sa.text("SELECT name, colour, tenant_id FROM content_review_themes ORDER BY ordinal")).all()
        assert [t[0] for t in themes] == ["Blue team", "Red team", "DFIR", "Network", "ICS / OT", "Cloud",
                                          "Malware", "Platform"]
        assert all(t[2] is None for t in themes) and themes[0][1] == "#3B5BDB"
        assert db.execute(sa.text("SELECT count(*) FROM courses")).scalar() == 1
        assert db.execute(sa.text("SELECT count(*) FROM content_review_items")).scalar() == 0

    from app.content_review import ContentReviewItem

    with Session(engine) as db:  # the ORM's model is the migration's schema
        db.add(ContentReviewItem(id=uuid.uuid4(), tenant_id=tenant, item_type="course", item_id=str(course),
                                 title="C101", theme_ids=[], stale_reasons=[], due_at=datetime.now(UTC)))
        db.commit()

    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM courses")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
