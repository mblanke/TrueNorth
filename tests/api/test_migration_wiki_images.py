"""c5e7a9b1d3f4: per-user notification preferences, for ticket email (#378).

Upgrade from its parent adds ``wiki_images`` with no rows; a second upgrade is a no-op; downgrade drops it and re-upgrade restores it. SQLite here;
the PostgreSQL chain runs in CI (test_migration_populated_upgrade.py and
test_migration_downgrades.py with TEST_POSTGRES_ADMIN_URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "b4d6f8a0c2e4", "c5e7a9b1d3f4"
COLUMNS = {
    "id",
    "tenant_id",
    "page_id",
    "filename",
    "content_type",
    "size_bytes",
    "width",
    "height",
    "object_key",
    "uploaded_by",
    "created_at",
    "updated_at",
    "deleted_at",
}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def _table(engine) -> set[str] | None:
    insp = sa.inspect(engine)
    if "wiki_images" not in insp.get_table_names():
        return None
    return {c["name"] for c in insp.get_columns("wiki_images")}


def test_the_revision_chains_onto_the_previous_head():
    text = (API / f"alembic/versions/{REVISION}_add_wiki_images.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_an_empty_table_and_downgrade_removes_it(tmp_path):
    url = f"sqlite:///{tmp_path / 'wikiimg.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    assert _table(engine) is None

    _alembic(url, "upgrade", REVISION)
    assert _table(engine) == COLUMNS
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT COUNT(*) FROM wiki_images")).scalar() == 0

    _alembic(url, "upgrade", REVISION)  # already there: a no-op, not an error
    assert _table(engine) == COLUMNS

    _alembic(url, "downgrade", PARENT)
    assert _table(engine) is None
    _alembic(url, "upgrade", REVISION)
    assert _table(engine) == COLUMNS
