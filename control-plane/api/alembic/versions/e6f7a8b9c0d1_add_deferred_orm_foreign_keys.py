"""add the foreign keys b0c1d2e3f4a5 has to defer on Postgres

Revision ID: e6f7a8b9c0d1
Revises: a3b4c5d6e7f8
Create Date: 2026-09-27 10:00:00.000000

b0c1d2e3f4a5 builds the ORM-only tables right after the initial schema, from the
live models. Three of their foreign keys point at tables that later revisions
create:

    courses.qualification_id                    -> qualifications (b8c9d0e1f2a3)
    course_modules.po_id                        -> performance_objectives (b8c9d0e1f2a3)
    competency_auto_assessments.quiz_attempt_id -> quiz_attempts (a7b8c9d0e1f2)

Postgres refuses those at CREATE time, so b0c1d2e3f4a5 now leaves them out. The
revisions that create the targets find the columns already there and skip them.
This revision, at the head, where every target exists, adds each key the models
define on those tables that the database does not have yet.

On any database that has been running already, the API's `create_all()` made these
tables with their keys, and this is a no-op. SQLite is skipped: b0c1d2e3f4a5 keeps
every key there, and SQLite cannot add a constraint to an existing table anyway.

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from app.models import Base

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _orm_only_tables() -> tuple[str, ...]:
    """The table list from b0c1d2e3f4a5, so the two revisions cannot disagree."""
    path = Path(__file__).with_name("b0c1d2e3f4a5_reconcile_orm_only_tables.py")
    spec = importlib.util.spec_from_file_location("_reconcile_orm_only_tables", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ORM_ONLY_TABLES


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name in _orm_only_tables():
        table = Base.metadata.tables.get(name)
        if table is None or name not in existing:
            continue
        present = {(tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in inspector.get_foreign_keys(name)}
        have_columns = {c["name"] for c in inspector.get_columns(name)}
        for fkc in table.foreign_key_constraints:
            columns = [c.name for c in fkc.columns]
            target = fkc.elements[0].column.table.name
            if target not in existing or (tuple(columns), target) in present:
                continue
            if not set(columns) <= have_columns:
                continue  # a later revision adds the column, with its key (b0c1d2e3f4a5 LATER_COLUMNS)
            op.create_foreign_key(
                f"{name}_{'_'.join(columns)}_fkey",  # the name Postgres gives create_all()'s keys
                name,
                target,
                columns,
                [fk.column.name for fk in fkc.elements],
            )


# The tables that exist when b0c1d2e3f4a5 runs on an empty database: the initial schema
# (3731bf01ced3, frozen) plus the ones b0c1d2e3f4a5 creates. A key from an ORM-only table
# to anything else is one b0c1d2e3f4a5 defers on Postgres and this revision adds.
INITIAL_SCHEMA_TABLES: tuple[str, ...] = (
    "tenants",
    "users",
    "teams",
    "team_memberships",
    "templates",
    "scenarios",
    "ranges",
    "exercises",
    "objectives",
    "after_action_reports",
    "audit_logs",
)


def downgrade() -> None:
    """Drop the keys b0c1d2e3f4a5 defers on Postgres, returning the schema to what the
    chain builds at a3b4c5d6e7f8.

    That is the previous revision's schema whichever way the database was built, so on a
    database ``create_all()`` made, the keys are dropped too (the columns and their data
    stay). SQLite is skipped, as in the upgrade: b0c1d2e3f4a5 keeps every key there.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    orm_only = _orm_only_tables()
    available_at_b0c1 = set(INITIAL_SCHEMA_TABLES) | set(orm_only)
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name in orm_only:
        table = Base.metadata.tables.get(name)
        if table is None or name not in existing:
            continue
        deferred = {
            (tuple(c.name for c in fkc.columns), fkc.elements[0].column.table.name)
            for fkc in table.foreign_key_constraints
            if fkc.elements[0].column.table.name not in available_at_b0c1
        }
        for fk in inspector.get_foreign_keys(name):
            if fk.get("name") and (tuple(fk["constrained_columns"]), fk["referred_table"]) in deferred:
                op.drop_constraint(fk["name"], name, type_="foreignkey")
