"""648ef1fe0b6c adds the AD status sync tables, and only those (#290).

Additive and empty: ``user_last_seen`` is not backfilled (an account with no row counts from
the first sync run). Downgrade drops the three tables; re-upgrade restores them. SQLite here;
the PostgreSQL chain runs in CI (test_migration_populated_upgrade.py, test_migration_downgrades.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "57e2c4b65b98", "648ef1fe0b6c"
TABLES = {"ad_status_sync_runs", "ad_status_sync_results", "user_last_seen"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_previous_head():
    text = (API / f"alembic/versions/{REVISION}_ad_status_sync.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_tables_empty_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'adsync.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    with engine.connect() as db:
        assert all(db.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar() == 0 for t in TABLES)
    # The unique slot is how exactly one replica claims the night's run.
    assert any(c["column_names"] == ["slot"] for c in insp.get_unique_constraints("ad_status_sync_runs")) or any(
        i["column_names"] == ["slot"] and i["unique"] for i in insp.get_indexes("ad_status_sync_runs"))
    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    _alembic(url, "upgrade", REVISION)
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
