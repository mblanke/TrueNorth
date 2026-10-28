"""c3318a0d7e51 maps the OU and security-group types the Users page used to offer onto the
API's enums (#318, review of #322): department -> custom, team -> custom, role -> role_based.
A row with a legacy type could not be saved from the page after the enums were enforced.
Other values are left alone, and the migration is idempotent. SQLite here; the PostgreSQL
chain runs in CI (test_migration_populated_upgrade.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS = Path(__file__).resolve().parents[2] / "control-plane/api/alembic/versions"
REVISION, PARENT = "c3318a0d7e51", "7c1d2e9f4a60"


def _module():
    path = next(VERSIONS.glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location("mig_c3318", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_chains_onto_the_previous_head_and_does_not_fork():
    mod = _module()
    assert mod.revision == REVISION and mod.down_revision == PARENT
    # Later migrations chain after it (one head overall is test_alembic_revisions' check);
    # two of them on the same parent would fork the history.
    children = [p for p in VERSIONS.glob("*.py") if f'down_revision: str | None = "{REVISION}"' in p.read_text()]
    assert len(children) <= 1


def test_legacy_types_are_mapped_and_the_rest_left_alone():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE organizational_units (id INTEGER PRIMARY KEY, ou_type VARCHAR(20))"))
        conn.execute(sa.text("CREATE TABLE security_groups (id INTEGER PRIMARY KEY, group_type VARCHAR(20))"))
        conn.execute(sa.text("INSERT INTO organizational_units (ou_type) VALUES ('department'), ('team'), "
                             "('division'), ('custom'), ('country')"))
        conn.execute(sa.text("INSERT INTO security_groups (group_type) VALUES ('role'), ('access'), "
                             "('role_based'), ('training')"))
        mod = _module()
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()  # idempotent
        ous = conn.execute(sa.text("SELECT ou_type FROM organizational_units ORDER BY id")).scalars().all()
        groups = conn.execute(sa.text("SELECT group_type FROM security_groups ORDER BY id")).scalars().all()
    assert ous == ["custom", "custom", "division", "custom", "country"]
    assert groups == ["role_based", "access", "role_based", "training"]
