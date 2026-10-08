"""add the columns the models gained that no migration ever added

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-27 11:00:00.000000

Run the chain on an empty Postgres database and diff the result against the ORM,
and 35 columns are missing from seven tables the chain does create. Among them
are `users.first_name` and fifteen other user columns, and `range_snapshots.snapshot_state`
and `snapshot_data`. The models select every mapped column, so on a migrated
database the first query for a user fails, and nobody can sign in.

Nobody noticed because every database in use was built by the API's `create_all()`,
which adds whole tables with every column. The installer runs
`alembic upgrade head` with `DB_AUTO_CREATE=false`, which is exactly the path that
leaves the columns out. It had not got this far before, because the chain died
at b0c1d2e3f4a5 first.

Definitions come from the ORM, like b0c1d2e3f4a5; the list of columns is frozen
here. Each column is skipped when it already exists, so on a database that
`create_all()` built, this revision changes nothing.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import Base

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: the columns the chain did not create, as of this revision.
MISSING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "first_name"),
    ("users", "last_name"),
    ("users", "rank"),
    ("users", "service_branch"),
    ("users", "nation_id"),
    ("users", "clearance_level"),
    ("users", "unit"),
    ("users", "callsign"),
    ("users", "avatar_url"),
    ("users", "source"),
    ("users", "ad_object_guid"),
    ("users", "ad_distinguished_name"),
    ("users", "last_synced_at"),
    ("users", "auth_method_preference"),
    ("users", "timezone"),
    ("users", "deleted_at"),
    ("teams", "description"),
    ("teams", "team_type"),
    ("teams", "color_hex"),
    ("teams", "ou_id"),
    ("teams", "nation_id"),
    ("teams", "max_members"),
    ("teams", "is_persistent"),
    ("teams", "exercise_id"),
    ("team_memberships", "position"),
    ("team_memberships", "joined_at"),
    ("templates", "deleted_at"),
    ("scenarios", "deleted_at"),
    ("ranges", "deleted_at"),
    ("range_snapshots", "snapshot_state"),
    ("range_snapshots", "snapshot_data"),
    ("range_snapshots", "range_state_at_snapshot"),
    ("range_snapshots", "tenant_id"),
    ("range_snapshots", "updated_at"),
    ("exercises", "deleted_at"),
)

# NOT NULL columns with no default at all: how to fill rows that predate them.
# A snapshot belongs to its range's tenant; the state only picks whether restore
# powers the VMs on, and a snapshot can only be taken of a ready or stopped range.
BACKFILL: dict[tuple[str, str], str] = {
    ("range_snapshots", "tenant_id"): (
        "UPDATE range_snapshots SET tenant_id = ranges.tenant_id FROM ranges "
        "WHERE ranges.id = range_snapshots.range_id AND range_snapshots.tenant_id IS NULL"
    ),
    ("range_snapshots", "range_state_at_snapshot"): (
        "UPDATE range_snapshots SET range_state_at_snapshot = 'ready' WHERE range_state_at_snapshot IS NULL"
    ),
}


def _scalar_default(column: sa.Column):
    """The ORM's Python-side default as a server default, so existing rows get it."""
    if column.default is None or not column.default.is_scalar:
        return None
    value = column.default.arg
    if isinstance(value, bool):
        return sa.true() if value else sa.false()
    return str(value)


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table_name, column_name in MISSING_COLUMNS:
        if table_name not in tables:
            continue
        if column_name in {c["name"] for c in inspector.get_columns(table_name)}:
            continue
        orm = Base.metadata.tables[table_name].columns[column_name]
        server_default = orm.server_default.arg if orm.server_default is not None else None
        fill = None if server_default is not None else _scalar_default(orm)
        # With a default, the column can be NOT NULL from the start: Postgres fills
        # existing rows with it. Without one it starts nullable and is backfilled.
        filled = server_default is not None or fill is not None
        op.add_column(
            table_name,
            sa.Column(
                column_name,
                orm.type,
                nullable=orm.nullable or not filled,
                server_default=server_default if server_default is not None else fill,
            ),
        )
        if orm.index:
            op.create_index(f"ix_{table_name}_{column_name}", table_name, [column_name])
        if sqlite:
            continue  # SQLite cannot alter a column or add a constraint in place

        if fill is not None:
            # The default was only for existing rows. The ORM supplies it on insert.
            op.alter_column(table_name, column_name, server_default=None)
        if not orm.nullable and not filled:
            if (table_name, column_name) in BACKFILL:
                op.execute(BACKFILL[(table_name, column_name)])
            nulls = bind.execute(sa.text(f"SELECT count(*) FROM {table_name} WHERE {column_name} IS NULL")).scalar()
            if not nulls:
                op.alter_column(table_name, column_name, existing_type=orm.type, nullable=False)
        for fk in orm.foreign_keys:
            op.create_foreign_key(
                f"{table_name}_{column_name}_fkey",
                table_name,
                fk.column.table.name,
                [column_name],
                [fk.column.name],
            )
        if orm.unique:
            op.create_unique_constraint(f"{table_name}_{column_name}_key", table_name, [column_name])


def downgrade() -> None:
    """Drop the columns this revision adds, returning the schema to what the chain builds
    at e6f7a8b9c0d1.

    That is the schema of the previous revision whichever way the database was built, so
    on a database ``create_all()`` made, where these columns predate this revision, the
    downgrade drops them and their data too, as any downgrade of an added column does.
    A column already gone (a later revision's downgrade took it) is skipped. Postgres
    drops a column's index, foreign key and unique constraint with it; SQLite rebuilds
    the table (batch mode), which also covers columns create_all() made with inline keys.
    """
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    by_table: dict[str, list[str]] = {}
    for table_name, column_name in reversed(MISSING_COLUMNS):
        if table_name not in tables:
            continue
        if column_name in {c["name"] for c in inspector.get_columns(table_name)}:
            by_table.setdefault(table_name, []).append(column_name)

    for table_name, columns in by_table.items():
        if sqlite:
            # Postgres drops a dropped column's indexes itself; SQLite's batch copy would
            # try to recreate them on the new table and fail.
            for index in inspector.get_indexes(table_name):
                if set(index["column_names"]) & set(columns):
                    op.drop_index(index["name"], table_name=table_name)
            with op.batch_alter_table(table_name, recreate="always") as batch:
                for column_name in columns:
                    batch.drop_column(column_name)
        else:
            for column_name in columns:
                op.drop_column(table_name, column_name)
