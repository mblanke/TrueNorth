"""e6f7a8b9c0d1 and f7a8b9c0d1e2 downgrade for real, and the chain round-trips through them.

Both used to have ``pass`` downgrades, so ``alembic downgrade`` below them left the 35
columns f7a8b9c0d1e2 adds and the three foreign keys e6f7a8b9c0d1 adds in place: the
database claimed to be at a3b4c5d6e7f8 with a schema that revision never had, and the
next upgrade skipped them as already present. Now each drops what it adds.

The proof is schema equality: base -> a3b4c5d6e7f8 (S0) -> f7a8b9c0d1e2 -> back (must
equal S0), then -> head (S1) -> a3b4c5d6e7f8 (must equal S0) -> head (must equal S1), so
every revision from a3b4c5d6e7f8 to head undoes exactly what it does. On SQLite always;
on PostgreSQL when TEST_POSTGRES_ADMIN_URL names a superuser (CI's test-python job).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
BEFORE = "a3b4c5d6e7f8"  # e6f7a8b9c0d1's down_revision
AFTER = "f7a8b9c0d1e2"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def _missing_columns() -> tuple[tuple[str, str], ...]:
    path = API / "alembic/versions/f7a8b9c0d1e2_add_orm_columns_no_migration_added.py"
    spec = importlib.util.spec_from_file_location("_f7a8", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MISSING_COLUMNS


def _schema(url: str) -> dict:
    """Tables, their columns (name, nullable), foreign keys and indexes."""
    import sqlalchemy as sa

    engine = sa.create_engine(url)
    try:
        insp = sa.inspect(engine)
        out = {}
        for table in sorted(insp.get_table_names()):
            if table == "alembic_version":
                continue
            out[table] = {
                "columns": sorted((c["name"], bool(c["nullable"])) for c in insp.get_columns(table)),
                "fks": sorted(
                    (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                    for fk in insp.get_foreign_keys(table)
                ),
                "indexes": sorted((tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)),
            }
        return out
    finally:
        engine.dispose()


def _diff(a: dict, b: dict) -> dict:
    return {
        t: {k: (a.get(t, {}).get(k), b.get(t, {}).get(k)) for k in ("columns", "fks", "indexes")
            if a.get(t, {}).get(k) != b.get(t, {}).get(k)}
        for t in sorted(set(a) | set(b))
        if a.get(t) != b.get(t)
    }


def _round_trip(url: str, *, foreign_keys: bool) -> None:
    """BEFORE -> AFTER -> BEFORE -> head -> BEFORE -> head, with schema equality each time.

    The full trip to head and back needs every revision after BEFORE to undo exactly what
    it did; until 2026-10-08 the scheduler revisions could not (b0c1d2e3f4a5 built
    scheduled_events with their columns, so they skipped adding them and then dropped
    them on downgrade; on SQLite the drop of a keyed column failed outright).
    """
    _alembic(url, "upgrade", BEFORE)
    before = _schema(url)
    # b0c1d2e3f4a5 leaves the scheduler revisions' columns out, and keeps the table's other keys.
    events = before["scheduled_events"]
    assert not {"instructor_id", "course_id", "exercise_id", "sequence"} & {c for c, _ in events["columns"]}
    assert (("tenant_id",), "tenants", ("id",)) in events["fks"]
    _alembic(url, "upgrade", AFTER)
    after = _schema(url)
    for table, column in _missing_columns():
        assert column in {c for c, _ in after[table]["columns"]}, f"{table}.{column} missing at {AFTER}"
    if foreign_keys:
        assert (("qualification_id",), "qualifications", ("id",)) in after["courses"]["fks"]

    _alembic(url, "downgrade", BEFORE)
    down = _schema(url)
    assert not _diff(before, down), f"downgrade to {BEFORE} left a different schema: {_diff(before, down)}"
    for table, column in _missing_columns():
        assert column not in {c for c, _ in down[table]["columns"]}, f"{table}.{column} survived the downgrade"
    if foreign_keys:
        assert ("qualification_id",) not in {fk[0] for fk in down["courses"]["fks"]}

    _alembic(url, "upgrade", "head")
    head = _schema(url)
    _alembic(url, "downgrade", BEFORE)
    down = _schema(url)
    assert not _diff(before, down), f"downgrade from head to {BEFORE} differs: {_diff(before, down)}"
    _alembic(url, "upgrade", "head")
    again = _schema(url)
    assert not _diff(head, again), f"re-upgrade to head differs: {_diff(head, again)}"


@pytest.mark.slow
def test_upgrade_downgrade_upgrade_round_trips_on_sqlite(tmp_path):
    _round_trip(f"sqlite:///{tmp_path / 'roundtrip.db'}", foreign_keys=False)


@pytest.mark.slow
def test_upgrade_downgrade_upgrade_round_trips_on_postgres():
    import sqlalchemy as sa

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run the round trip against Postgres")
    name = f"tn_roundtrip_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = sa.engine.make_url(admin_url).set(database=name).render_as_string(hide_password=False)
        _round_trip(url, foreign_keys=True)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
