"""The migration chain must be able to build the schema on its own.

For most of this repo's life it could not. Of the 69 tables the ORM defines,
**37 were created by no migration at all** — they existed only because
`app/main.py`'s lifespan calls `Base.metadata.create_all()` on every API start.
In development the schema was therefore always already present, so nothing ever
exercised the chain, and `alembic upgrade head` against an empty database
aborted at `a7b8c9d0e1f2` trying to ALTER a table nothing had created.

That is invisible until the day it matters: a production installer that sets
`DB_AUTO_CREATE=false` and trusts migrations would have produced a database
silently missing `nations`, `courses`, `enrollments` and `hypervisor_connections`.

These tests make the gap fail here instead.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from _shared import _run_upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
API = REPO_ROOT / "control-plane/api"
VERSIONS = API / "alembic/versions"
sys.path.insert(0, str(API))

CREATE_TABLE = re.compile(r'op\.create_table\(\s*["\']([a-z_]+)["\']')

# Tables created by migrations that have no ORM model. They predate this guard
# and are reached through raw SQL rather than the ORM; listed so the check below
# can be exact rather than one-directional.
MIGRATION_ONLY_TABLES = {
    "api_keys",
    "audit_events",
    "custom_permissions",
    "leaderboard_entries",
    "objective_results",
    "scoring_results",
}


def _tables_created_by_migrations() -> set[str]:
    created: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        if path.name.startswith("__"):
            continue
        created |= set(CREATE_TABLE.findall(path.read_text(encoding="utf-8")))
    return created


def _orm_tables() -> set[str]:
    from app.models import Base

    return set(Base.metadata.tables)


def test_every_orm_table_is_created_by_a_migration():
    """No table may rely on create_all() to exist.

    `b0c1d2e3f4a5` creates the set that historically did. A model added without
    a migration fails here rather than at somebody's first production install.
    """
    from app.models import Base  # noqa: F401  (import cost is the point of the fixture)

    orm = _orm_tables()
    created = _tables_created_by_migrations()

    # b0c1d2e3f4a5 builds its tables from ORM metadata rather than literal
    # create_table calls, so credit its explicit frozen list.
    spec = (VERSIONS / "b0c1d2e3f4a5_reconcile_orm_only_tables.py").read_text(encoding="utf-8")
    block = spec.split("ORM_ONLY_TABLES", 1)[1].split(")", 1)[0]
    created |= set(re.findall(r'"([a-z_]+)"', block))

    missing = sorted(orm - created)
    assert not missing, (
        f"{len(missing)} ORM table(s) are created by no migration:\n  "
        + "\n  ".join(missing)
        + "\n\nThese exist only because the API's create_all() makes them at "
        "startup. `alembic upgrade head` on an empty database will not produce "
        "them, so any deployment that trusts migrations gets a broken schema. "
        "Add a migration, or extend ORM_ONLY_TABLES in b0c1d2e3f4a5."
    )


def test_no_unexpected_migration_only_tables():
    """The reverse direction: a migration creating a table with no model."""
    stray = sorted(_tables_created_by_migrations() - _orm_tables() - MIGRATION_ONLY_TABLES)
    assert not stray, (
        f"Migration(s) create table(s) with no ORM model: {stray}. Either add the "
        "model or record them in MIGRATION_ONLY_TABLES with a reason."
    )


@pytest.mark.slow
def test_upgrade_head_works_on_an_empty_database(tmp_path):
    """The end-to-end proof: base -> head with nothing pre-created.

    This is the check that would have caught the original breakage. It runs
    alembic in a subprocess against a scratch SQLite file so it cannot be
    contaminated by the in-memory schema the rest of the suite builds with
    create_all().
    """
    db = tmp_path / "fresh.db"
    _run_upgrade_head(f"sqlite:///{db}")
    _assert_schema_matches_orm(f"sqlite:///{db}")


def _assert_schema_matches_orm(database_url: str, *, foreign_keys: bool = False) -> None:
    """Every ORM table and column exists, and optionally every foreign key.

    Checking tables alone missed 35 columns the models had gained with no migration,
    among them `users.first_name`: a migrated database could not load a single user.
    """
    import sqlalchemy as sa
    from app.models import Base

    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        built = set(inspector.get_table_names())
        missing = sorted(_orm_tables() - built)
        assert not missing, f"migrated schema is missing ORM table(s): {missing}"

        missing_columns = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if column.name not in {c["name"] for c in inspector.get_columns(table.name)}
        ]
        assert not missing_columns, f"migrated schema is missing ORM column(s): {missing_columns}"

        if foreign_keys:
            have = {(t, tuple(fk["constrained_columns"])) for t in built for fk in inspector.get_foreign_keys(t)}
            want = {
                (table.name, tuple(c.name for c in fkc.columns))
                for table in Base.metadata.tables.values()
                for fkc in table.foreign_key_constraints
            }
            assert not want - have, f"migrated schema is missing foreign key(s): {sorted(want - have)}"
    finally:
        engine.dispose()


@pytest.mark.slow
def test_upgrade_head_works_on_an_empty_postgres_database():
    """The same proof on Postgres, which is what production runs.

    SQLite let two breakages through: it accepts a FOREIGN KEY to a table that does
    not exist yet (b0c1d2e3f4a5 made three), and it has no enum types, so it never
    saw b0c1d2e3f4a5 create every enum and a later revision fail to create one again.

    Set TEST_POSTGRES_ADMIN_URL to a superuser URL, for example
    postgresql+psycopg://postgres@127.0.0.1:5432/postgres. The test creates and drops
    its own scratch database.
    """
    import os
    import uuid

    import sqlalchemy as sa

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run the chain against Postgres")

    name = f"tn_migrate_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = sa.engine.make_url(admin_url).set(database=name).render_as_string(hide_password=False)
        _run_upgrade_head(url)
        _assert_schema_matches_orm(url, foreign_keys=True)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
