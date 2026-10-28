"""7c1d2e9f4a60 adds the content port's two tables, and only those (docs/content-port.md).

Upgrade from its parent onto a database holding a template: the tables appear with their
keys, nothing is written, and an import record and an item link can be written through the
ORM. Downgrade drops both and keeps the template; re-upgrade restores them. SQLite here; the
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
PARENT, REVISION = "3e8b1c7a9d42", "7c1d2e9f4a60"
TABLES = {"content_port_imports", "content_port_items"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_single_head():
    text = (API / f"alembic/versions/{REVISION}_content_port.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'port.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    from app.models import Template
    from sqlalchemy.orm import Session

    tmpl = uuid.uuid4()
    with Session(engine) as db:
        db.add(Template(id=tmpl, name="Port lab", version="1.0", yaml="name: x\n"))
        db.commit()
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("content_port_imports")} == {
        ("bundle_id",)}
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("content_port_items")} == {
        ("item_type", "local_id")}
    with engine.connect() as db:
        for table in TABLES:
            assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0, table

    from app.content_port import ContentPortImport, ContentPortItem

    now = datetime.now(UTC)
    with Session(engine) as db:  # the ORM's models are the migration's schema
        imp = ContentPortImport(id=uuid.uuid4(), bundle_id=uuid.uuid4(), key_id="0" * 16, signed_sha256="0" * 64,
                                exported_at=now, imported_at=now, shared=False, summary={"create": 1})
        db.add(imp)
        db.flush()
        db.add(ContentPortItem(id=uuid.uuid4(), item_type="range_template", local_id=str(tmpl), origin_id=str(tmpl),
                               origin_key_id="0" * 16, origin_content_sha256="0" * 64, exported_at=now,
                               import_id=imp.id, local_fingerprint="0" * 64, proof={}, golden_builds=[]))
        db.commit()

    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM templates")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
