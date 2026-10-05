"""Upgrading a populated production schema (PostgreSQL) loses nothing and runs the new code.

The empty-database chain test proves a fresh install. This proves the upgrade the plan's
migration contract asks for: a database at the deployed head (main, f7a8b9c0d1e2) with
tenants, templates, users and ranges in several states is upgraded to head with
DB_AUTO_CREATE=false semantics (Alembic only). Every row survives unchanged, the new
tables exist and are empty (they are additive; nothing is backfilled), the new code
works on the upgraded rows (a range operation, a network reservation), and the new
migrations downgrade and re-apply cleanly.

Runs when TEST_POSTGRES_ADMIN_URL names a superuser; skipped otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
DEPLOYED_HEAD = "f7a8b9c0d1e2"  # main's head when this work started (S0 baseline)
NEW_TABLES = ("wiki_spaces", "wiki_pages", "wiki_revisions", "range_operations", "network_reservations")


def _alembic(url: str, *args: str) -> None:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def _fill(table, given: dict) -> dict:
    """``given`` plus a plausible value for every other NOT NULL column without a default."""
    import sqlalchemy as sa

    row = dict(given)
    for col in table.columns:
        if col.name in row or col.nullable or col.server_default is not None or col.autoincrement is True:
            continue
        t = col.type
        if isinstance(t, sa.Enum) and t.enums:
            row[col.name] = t.enums[0]
        elif isinstance(t, (sa.Boolean,)):
            row[col.name] = False
        elif isinstance(t, (sa.Integer, sa.Numeric, sa.Float)):
            row[col.name] = 0
        elif isinstance(t, sa.DateTime):
            row[col.name] = datetime.now(UTC)
        elif isinstance(t, sa.JSON) or "JSON" in type(t).__name__.upper():
            row[col.name] = {}
        elif "UUID" in type(t).__name__.upper():
            row[col.name] = uuid.uuid4()
        else:
            row[col.name] = f"{col.name}-{uuid.uuid4().hex[:6]}"
    return row


def test_a_populated_deployed_database_upgrades_to_head_without_loss():
    import sqlalchemy as sa

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run the populated upgrade against Postgres")
    name = f"tn_upgrade_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    engine = sa.create_engine(url)
    try:
        _alembic(url, "upgrade", DEPLOYED_HEAD)

        meta = sa.MetaData()
        meta.reflect(engine, only=["tenants", "templates", "users", "ranges", "audit_logs"])
        t = meta.tables
        tenant_ids = [uuid.uuid4(), uuid.uuid4()]
        template_id = uuid.uuid4()
        range_rows = []
        with engine.begin() as conn:
            for i, tid in enumerate(tenant_ids):
                conn.execute(
                    t["tenants"].insert().values(_fill(t["tenants"], {"id": tid, "name": f"T{i}", "slug": f"t{i}"}))
                )
            conn.execute(
                t["templates"]
                .insert()
                .values(
                    _fill(
                        t["templates"],
                        {"id": template_id, "name": "Red v Blue", "yaml": "nodes: []\n", "tenant_id": tenant_ids[0]},
                    )
                )
            )
            conn.execute(
                t["users"]
                .insert()
                .values(
                    _fill(
                        t["users"],
                        {
                            "id": uuid.uuid4(),
                            "email": "i@example.test",
                            "keycloak_id": "kc-1",
                            "tenant_id": tenant_ids[0],
                        },
                    )
                )
            )
            for i, state in enumerate(("created", "ready", "provisioning", "destroyed", "failed")):
                row = _fill(
                    t["ranges"],
                    {
                        "id": uuid.uuid4(),
                        "name": f"range-{i}",
                        "template_id": template_id,
                        "tenant_id": tenant_ids[i % 2],
                        "state": state,
                        # A built range records its VMs; one never built has none (#33
                        # refuses to provision over recorded VMs).
                        "provisioner_output": '{"vms": [{"name": "web"}]}' if state in ("ready", "failed") else None,
                    },
                )
                range_rows.append(row)
                conn.execute(t["ranges"].insert().values(row))

        def snapshot():
            with engine.connect() as conn:
                return {
                    "tenants": sorted(map(tuple, conn.execute(sa.text("SELECT id, name, slug FROM tenants")))),
                    "ranges": sorted(
                        map(
                            tuple,
                            conn.execute(
                                sa.text(
                                    "SELECT id, name, CAST(state AS TEXT), tenant_id, provisioner_output FROM ranges"
                                )
                            ),
                        )
                    ),
                    "templates": sorted(map(tuple, conn.execute(sa.text("SELECT id, name, yaml FROM templates")))),
                    "users": sorted(map(tuple, conn.execute(sa.text("SELECT id, email, tenant_id FROM users")))),
                }

        before = snapshot()
        assert (len(before["tenants"]), len(before["ranges"]), len(before["users"])) == (2, 5, 1)
        _alembic(url, "upgrade", "head")
        assert snapshot() == before, "the upgrade changed or lost existing rows"
        inspector = sa.inspect(engine)
        for table in NEW_TABLES:
            assert table in inspector.get_table_names(), f"{table} missing after upgrade"
            with engine.connect() as conn:
                assert conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

        # The new code works on rows that existed before the upgrade.
        from app import network_inventory, range_ops
        from app.auth import CurrentUser
        from app.models import Range, UserRole
        from sqlalchemy.orm import Session

        created = next(r for r in range_rows if r["state"] == "created")
        user = CurrentUser(
            id=str(uuid.uuid4()),
            email="a@x",
            display_name="a",
            role=UserRole.admin,
            tenant_id=str(created["tenant_id"]),
            keycloak_id="kc",
        )
        with Session(engine) as s:
            op, rng, made = range_ops.accept(s, created["id"], user, "provision")
            got = network_inventory.reserve(
                s,
                rng,
                domain="vsphere:vc:pg",
                kind="noise_mgmt_ip",
                pool=network_inventory.parse_ip_pool("10.255.0.10-12"),
                holders=["ws01"],
            )
            s.commit()
            assert made and op.generation == 1 and got == {"ws01": "10.255.0.10"}
            assert s.get(Range, created["id"]).state.value == "provisioning"
            # The native enum gained the power states (d1e2f3a4b5c6).
            ready = next(r for r in range_rows if r["state"] == "ready")
            range_ops.accept(s, ready["id"], user.model_copy(update={"tenant_id": str(ready["tenant_id"])}), "stop")
            s.commit()
            assert s.get(Range, ready["id"]).state.value == "stopping"

        # The new migrations are additive and reversible: down to the deployed head and back.
        _alembic(url, "downgrade", DEPLOYED_HEAD)
        names = sa.inspect(engine).get_table_names()
        assert not set(NEW_TABLES) & set(names)
        with engine.connect() as conn:
            back = conn.execute(sa.text("SELECT CAST(state AS TEXT) FROM ranges WHERE id = :i"), {"i": ready["id"]})
            # d1e2f3a4b5c6's downgrade puts a range caught stopping where the previous code
            # showed it (it set stopped at once). range_operations itself is gone by then.
            assert back.scalar() == "stopped"
        after_down = snapshot()
        assert after_down["tenants"] == before["tenants"] and after_down["templates"] == before["templates"]
        _alembic(url, "upgrade", "head")
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
