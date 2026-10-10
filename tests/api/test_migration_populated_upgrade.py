"""Upgrading a populated production schema (PostgreSQL) loses nothing and runs the new code.

The empty-database chain test (test_migration_completeness.py) proves a fresh install.
This proves the upgrade: a database at an earlier deployed head (``f7a8b9c0d1e2``, the S0
baseline of 2026-10-04) holding tenants, templates, users, ranges in several states, an
exercise with objectives and a scheduled event is upgraded to head with
``DB_AUTO_CREATE=false`` semantics (Alembic only). Then:

* every pre-existing row survives unchanged, except the one documented data migration
  (``d2f3a4b5c6d7`` rewrites objective validator spellings to canonical names);
* every table the newer migrations add exists and is empty (they are additive), except
  ``xapi_legacy_identities``, which ``5a1e9c3d7b20`` fills from ``users`` by design (the
  legacy xAPI identity of every existing user, docs/xapi-conformance.md);
* columns added to populated tables take their defaults on the old rows;
* the new code works on rows that existed before the upgrade (a range operation, a
  network reservation, a power transition on the native ``rangestate`` enum);
* the newer migrations downgrade to the deployed head and re-apply cleanly.

Carved from hardening/s6-populated-upgrade (#22), adapted to main of 2026-10-07 (range_ops
and network_inventory packages, scheduler, wiki/tickets, noise, scenario runs, greyspace,
detection submissions). Runs when TEST_POSTGRES_ADMIN_URL names a superuser, as CI's
test-python job does; skipped otherwise.
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
DEPLOYED_HEAD = "f7a8b9c0d1e2"  # main's head at the S0 baseline (docs/hardening/)

# Tables the test fills at DEPLOYED_HEAD.
POPULATED_TABLES = [
    "tenants",
    "templates",
    "users",
    "ranges",
    "scenarios",
    "exercises",
    "objectives",
    "scheduled_events",
]

# Tables the migrations after DEPLOYED_HEAD create. A subset check: a new migration that
# adds a table does not break this test, but losing one of these does.
EXPECTED_NEW_TABLES = {
    "course_releases",
    "course_publications",
    "lab_sessions",
    "lab_network_leases",
    "range_leases",
    "range_operations",
    "network_reservations",
    "noise_profiles",
    "noise_agents",
    "scheduler_settings",
    "scheduler_feed_tokens",
    "wiki_spaces",
    "wiki_pages",
    "wiki_revisions",
    "tickets",
    "scenario_executions",
    "inject_records",
    "range_greyspace",
    "detection_submissions",
    "exercise_runs",
    "xapi_legacy_identities",
}
# New tables a migration fills on purpose (a documented data migration), and with what.
BACKFILLED_TABLES = {"xapi_legacy_identities"}

# b0c1d2e3f4a5 builds its frozen list of tables from the *live* ORM, so until 2026-10-08 a
# database taken to DEPLOYED_HEAD already had the columns the scheduler migrations after it
# add, and this test dropped them first. b0c1d2e3f4a5 now leaves them out (LATER_COLUMNS),
# as a database really deployed at that head did; the test checks that, so the upgrade's
# add_column path is the one exercised and the downgrade must restore that exact schema.
ORM_BUILT_AHEAD = {
    "scheduled_events": {
        "auto_exercise",
        "auto_provisioned",
        "course_id",
        "created_by",
        "exercise_id",
        "instructor_id",
        "reminded_at",
        "scenario_id",
        "sequence",
    },
}

# Old spelling -> canonical (d2f3a4b5c6d7; app/detections/names.py).
VALIDATOR_SPELLINGS = {
    "validate.opensearch.query": "opensearch_query",
    "validate_manual_ack": "manual_ack",
    "deliverable.check": "deliverable_check",
    "custom_validator": "custom_validator",  # not one of the three: left alone
}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
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
        elif isinstance(t, sa.Boolean):
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


def _columns(engine) -> dict[str, set[str]]:
    import sqlalchemy as sa

    inspector = sa.inspect(engine)
    return {table: {c["name"] for c in inspector.get_columns(table)} for table in inspector.get_table_names()}


def _rows(conn, sql: str) -> list[tuple]:
    import sqlalchemy as sa

    return sorted(map(tuple, conn.execute(sa.text(sql))), key=repr)


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
        for table, cols in ORM_BUILT_AHEAD.items():
            ahead = cols & {c["name"] for c in sa.inspect(engine).get_columns(table)}
            assert not ahead, f"{table} already has columns a later revision adds: {sorted(ahead)}"
        tables_at_deployed_head = set(sa.inspect(engine).get_table_names())
        columns_at_deployed_head = _columns(engine)

        meta = sa.MetaData()
        meta.reflect(engine, only=POPULATED_TABLES)
        t = meta.tables
        tenant_ids = [uuid.uuid4(), uuid.uuid4()]
        template_id = uuid.uuid4()
        user_id = uuid.uuid4()
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
                        {"id": user_id, "email": "i@example.test", "keycloak_id": "kc-1", "tenant_id": tenant_ids[0]},
                    )
                )
            )
            for i, state in enumerate(("created", "ready", "running", "provisioning", "destroyed", "failed")):
                row = _fill(
                    t["ranges"],
                    {
                        "id": uuid.uuid4(),
                        "name": f"range-{i}",
                        "template_id": template_id,
                        "tenant_id": tenant_ids[i % 2],
                        "state": state,
                        # A created range has built nothing yet; the others recorded a VM.
                        "provisioner_output": None if state == "created" else '{"vms": [{"name": "web"}]}',
                    },
                )
                range_rows.append(row)
                conn.execute(t["ranges"].insert().values(row))

            scenario_id = uuid.uuid4()
            conn.execute(
                t["scenarios"]
                .insert()
                .values(_fill(t["scenarios"], {"id": scenario_id, "name": "Phish", "tenant_id": tenant_ids[0]}))
            )
            exercise_id = uuid.uuid4()
            conn.execute(
                t["exercises"]
                .insert()
                .values(
                    _fill(
                        t["exercises"],
                        {
                            "id": exercise_id,
                            "name": "Ex 1",
                            "tenant_id": tenant_ids[0],
                            "scenario_id": scenario_id,
                            "range_id": range_rows[2]["id"],
                        },
                    )
                )
            )
            for i, spelling in enumerate(VALIDATOR_SPELLINGS):
                conn.execute(
                    t["objectives"]
                    .insert()
                    .values(
                        _fill(
                            t["objectives"],
                            {
                                "id": uuid.uuid4(),
                                "exercise_id": exercise_id,
                                "ref_id": f"OBJ-{i}",
                                "validator": spelling,
                            },
                        )
                    )
                )
            event_id = uuid.uuid4()
            conn.execute(
                t["scheduled_events"]
                .insert()
                .values(
                    _fill(
                        t["scheduled_events"],
                        {"id": event_id, "tenant_id": tenant_ids[0], "name": "Week 1"},
                    )
                )
            )

        def snapshot():
            with engine.connect() as conn:
                return {
                    "tenants": _rows(conn, "SELECT id, name, slug FROM tenants"),
                    "ranges": _rows(
                        conn, "SELECT id, name, CAST(state AS TEXT), tenant_id, provisioner_output FROM ranges"
                    ),
                    "templates": _rows(conn, "SELECT id, name, yaml FROM templates"),
                    "users": _rows(conn, "SELECT id, email, tenant_id FROM users"),
                    "scenarios": _rows(conn, "SELECT id, name, tenant_id FROM scenarios"),
                    "exercises": _rows(conn, "SELECT id, name, CAST(state AS TEXT), range_id FROM exercises"),
                    "objectives": _rows(conn, "SELECT id, exercise_id, ref_id FROM objectives"),
                    "scheduled_events": _rows(conn, "SELECT id, tenant_id FROM scheduled_events"),
                }

        before = snapshot()
        assert [len(before[k]) for k in ("tenants", "ranges", "users", "objectives", "scheduled_events")] == [
            2,
            6,
            1,
            len(VALIDATOR_SPELLINGS),
            1,
        ]

        _alembic(url, "upgrade", "head")

        assert snapshot() == before, "the upgrade changed or lost existing rows"
        tables_at_head = set(sa.inspect(engine).get_table_names())
        added = tables_at_head - tables_at_deployed_head
        assert not tables_at_deployed_head - tables_at_head, "the upgrade dropped a table"
        assert added >= EXPECTED_NEW_TABLES, f"missing after upgrade: {sorted(EXPECTED_NEW_TABLES - added)}"
        with engine.connect() as conn:
            populated = {
                table: n
                for table in sorted(added - BACKFILLED_TABLES)
                if (n := conn.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar())
            }
            assert not populated, f"additive migrations backfilled rows: {populated}"
            # 5a1e9c3d7b20: each existing user's pre-switch xAPI identity, and nothing else.
            legacy = conn.execute(sa.text("SELECT user_id, legacy_mbox FROM xapi_legacy_identities")).all()
            assert [(str(u), m) for u, m in legacy] == [(str(user_id), "mailto:i@example.test")]
            # The one data migration: objective validators are now canonical.
            validators = dict(conn.execute(sa.text("SELECT ref_id, validator FROM objectives")).all())
            assert sorted(validators.values()) == sorted(VALIDATOR_SPELLINGS.values())
            # Columns added to a populated table take their defaults on the old row.
            event = conn.execute(
                sa.text(
                    "SELECT auto_provisioned, auto_exercise, sequence, course_id, reminded_at "
                    "FROM scheduled_events WHERE id = :i"
                ),
                {"i": event_id},
            ).one()
            assert tuple(event) == (False, False, 0, None, None)

        # The new code works on rows that existed before the upgrade.
        from app import network_inventory, range_ops  # noqa: F401  (registers the tables)
        from app.auth import CurrentUser
        from app.models import Range, UserRole
        from app.range_ops import service as range_ops_service
        from sqlalchemy.orm import Session

        created = next(r for r in range_rows if r["state"] == "created")
        running = next(r for r in range_rows if r["state"] == "running")

        def admin_of(row) -> CurrentUser:
            return CurrentUser(
                id=str(uuid.uuid4()),
                email="a@x",
                display_name="a",
                role=UserRole.admin,
                tenant_id=str(row["tenant_id"]),
                keycloak_id="kc",
            )

        with Session(engine) as s:
            op, rng, made = range_ops_service.accept(s, created["id"], admin_of(created), "provision")
            got = network_inventory.reserve(
                s,
                rng,
                domain="vsphere:vc:pg",
                kind="noise_mgmt_ip",
                pool=network_inventory.parse_ip_pool("10.255.0.10-12"),
                holders=["ws01"],
            )
            s.commit()
            assert made and op.generation == 1 and op.status == "pending"
            assert got == {"ws01": "10.255.0.10"}
            assert s.get(Range, created["id"]).state.value == "provisioning"
            # The native enum gained the power states (e3f4a5b6c7d8).
            range_ops_service.accept(s, running["id"], admin_of(running), "stop")
            s.commit()
            assert s.get(Range, running["id"]).state.value == "stopping"

        # The newer migrations are reversible: down to the deployed head and back.
        _alembic(url, "downgrade", DEPLOYED_HEAD)
        assert set(sa.inspect(engine).get_table_names()) == tables_at_deployed_head
        columns_after_down = _columns(engine)
        changed = {
            table: sorted(columns_at_deployed_head[table] ^ columns_after_down[table])
            for table in columns_at_deployed_head
            if columns_at_deployed_head[table] != columns_after_down[table]
        }
        assert not changed, f"downgrade did not restore the deployed head's columns: {changed}"
        with engine.connect() as conn:
            back = conn.execute(sa.text("SELECT CAST(state AS TEXT) FROM ranges WHERE id = :i"), {"i": running["id"]})
            assert back.scalar() == "stopped", "a range caught stopping downgrades to what the old code showed"
        after_down = snapshot()
        for key in ("tenants", "templates", "users", "scenarios", "exercises", "objectives", "scheduled_events"):
            assert after_down[key] == before[key], f"{key} changed across upgrade + downgrade"
        _alembic(url, "upgrade", "head")
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
