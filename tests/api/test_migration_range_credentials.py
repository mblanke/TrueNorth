"""c8d9e0f1a2b3 adds range_credentials and wiki_spaces.system_owned, and only that (ADR 0012).

Upgrade from its parent onto a database that already holds a tenant, a template, a range
and a wiki space: the table appears empty with its unique key and indexes, the existing
space is not system-owned, the existing rows are untouched, and a sealed row can be
written and read back through the ORM. Downgrade drops both again, keeping the space;
re-upgrade restores them. SQLite here; test_migration_populated_upgrade.py covers
PostgreSQL in CI (TEST_POSTGRES_ADMIN_URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "b7c8d9e0f1a2", "c8d9e0f1a2b3"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


@pytest.mark.slow
def test_upgrade_adds_an_empty_sealed_table_and_downgrade_removes_it(tmp_path):
    url = f"sqlite:///{tmp_path / 'creds.db'}"
    _alembic(url, "upgrade", PARENT)
    from app.models import Range, RangeState, Template, Tenant
    from sqlalchemy.orm import Session

    engine = sa.create_engine(url)
    tenant, template, rng = (uuid.uuid4() for _ in range(3))
    with Session(engine) as db:  # the parent revision's schema is the ORM's, less the new table
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Template(id=template, name="tp", version="1", yaml="name: tp", tenant_id=tenant))
        db.flush()
        db.add(Range(id=rng, name="r", template_id=template, tenant_id=tenant, state=RangeState.ready))
        db.commit()
    with engine.begin() as db:  # a person's space, before system_owned existed
        db.execute(sa.text("INSERT INTO wiki_spaces (id, tenant_id, name, slug, description, icon, visibility, "
                           "is_archived) VALUES (:id, :t, 'Ranges', 'ranges', '', 'folder', 'all', 0)"),
                   {"id": str(uuid.uuid4()), "t": str(tenant)})
    assert "range_credentials" not in sa.inspect(engine).get_table_names()

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("range_credentials")}
    assert set(cols) == {"id", "tenant_id", "range_id", "machine", "username", "password", "generated",
                         "student_visible", "applied", "role", "notes", "created_at", "updated_at"}
    assert not {c["name"]: c for c in insp.get_columns("wiki_spaces")}["system_owned"]["nullable"]
    with engine.connect() as db:  # the existing space is a person's, and stays as it was
        row = db.execute(sa.text("SELECT visibility, system_owned FROM wiki_spaces")).one()
        assert (row[0], bool(row[1])) == ("all", False)
    assert not any(cols[c]["nullable"] for c in ("tenant_id", "range_id", "machine", "username", "password"))
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("range_credentials")} == {
        ("range_id", "machine", "username")}
    assert {i["name"] for i in insp.get_indexes("range_credentials")} >= {
        "ix_range_credentials_range", "ix_range_credentials_tenant"}
    fks = {fk["referred_table"]: fk for fk in insp.get_foreign_keys("range_credentials")}
    assert set(fks) == {"tenants", "ranges"} and fks["ranges"]["options"].get("ondelete") == "CASCADE"
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT count(*) FROM range_credentials")).scalar() == 0
        assert db.execute(sa.text("SELECT count(*) FROM ranges")).scalar() == 1

    # The new code on the migrated schema: a sealed write and an unsealed read.
    from app.range_credentials import RangeCredential

    with Session(engine) as db:
        assert db.get(Range, rng).state == RangeState.ready  # untouched
        db.add(RangeCredential(tenant_id=tenant, range_id=rng, machine="dc01",
                               username="Administrator", password="Pw-Sealed-1!", role="admin"))
        db.commit()
        assert db.query(RangeCredential).one().password == "Pw-Sealed-1!"
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT password FROM range_credentials")).scalar().startswith("tnsec:v1:")

    _alembic(url, "downgrade", PARENT)
    assert "range_credentials" not in sa.inspect(engine).get_table_names()
    assert "system_owned" not in {c["name"] for c in sa.inspect(engine).get_columns("wiki_spaces")}
    with engine.connect() as db:
        assert db.execute(sa.text("SELECT slug FROM wiki_spaces")).scalar() == "ranges"  # kept
    _alembic(url, "upgrade", "head")
    assert "range_credentials" in sa.inspect(engine).get_table_names()
    engine.dispose()
