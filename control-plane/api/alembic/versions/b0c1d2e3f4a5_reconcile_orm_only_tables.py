"""create the tables that only ever existed via create_all()

Revision ID: b0c1d2e3f4a5
Revises: 3731bf01ced3
Create Date: 2026-08-27 09:00:00.000000

`alembic upgrade head` has never worked on an empty database.

Of the 69 tables the ORM defines, **37 were created by no migration at all**.
They existed only because `app/main.py`'s lifespan calls
`Base.metadata.create_all()` on every API start, so in development the schema
was always already there and the gap was invisible. Running the chain against a
genuinely empty database aborted at `a7b8c9d0e1f2`, which alters
`competency_auto_assessments` — a table nothing had created.

That made the migration history unusable for a fresh install: the very thing an
installer needs it for.

This revision creates those 37 tables. It sits immediately after the initial
schema so that every later migration finds the tables it expects to alter, and
so that foreign keys pointing at them resolve.

**Why it builds them from the ORM rather than from literal DDL.** These tables
have no migration history to reproduce — the ORM is and always has been their
only definition. Writing out a hand-copied snapshot would add a second
definition that can drift from the models, which is the failure mode this is
fixing. The table list below is fixed and explicit, so this revision creates the
same set forever, even as the models grow.

`checkfirst=True` makes it a no-op on any existing database, where these tables
are already present from `create_all()`.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import Base

# revision identifiers, used by Alembic.
revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "3731bf01ced3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot: the tables no migration in this tree ever created, as of the
# revision that introduced this file. Deliberately explicit rather than computed
# — a migration must do the same thing every time it runs, and a list derived
# from live metadata would silently widen as the models grow.
ORM_ONLY_TABLES: tuple[str, ...] = (
    # Reference / directory
    "nations",
    "coalitions",
    "coalition_memberships",
    "organizational_units",
    "security_groups",
    "security_group_memberships",
    "auth_zone_policies",
    # Curriculum
    "courses",
    "course_modules",
    "learning_paths",
    "enrollments",
    "module_progress",
    # Competency
    "competencies",
    "competency_assertions",
    "competency_auto_assessments",
    "certifications",
    "learning_recommendations",
    # LMS / LTI
    "external_platforms",
    "external_activities",
    "lti_nonces",
    # Infrastructure
    "hypervisor_connections",
    "hypervisor_nodes",
    "hypervisor_pools",
    "storage_appliances",
    "storage_volumes",
    "network_devices",
    "kit_definitions",
    # AI
    "ai_backend_configs",
    "ai_fleet_nodes",
    "ai_model_routes",
    # Threat / detection
    "threat_intel_feeds",
    "threat_indicators",
    "detection_rules",
    # Exercises / ops
    "forged_exercises",
    "scheduled_events",
    "analyst_annotations",
    "shared_commands",
)

# Columns the models gained after this revision that a later revision adds. Built
# from the live ORM, these tables would already have them, the later revision would
# skip adding them, and its downgrade would then drop columns this revision made: a
# fresh chain could not go up to head and back. Leaving them out here makes each
# revision add, and remove, exactly its own columns.
LATER_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled_events": (
        "instructor_id",  # b5c6d7e8f9a0
        "created_by",  # b5c6d7e8f9a0
        "auto_provisioned",  # c6d7e8f9a0b1
        "reminded_at",  # c6d7e8f9a0b1
        "sequence",  # d7e8f9a0b1c2
        "course_id",  # e8f9a0b1c2d3
        "scenario_id",  # f9a0b1c2d3e4
        "exercise_id",  # f9a0b1c2d3e4
        "auto_exercise",  # f9a0b1c2d3e4
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    scratch = _as_of_this_revision()
    wanted = [scratch.tables[name] for name in ORM_ONLY_TABLES if name in scratch.tables and name not in existing]
    if not wanted:
        return
    if bind.dialect.name != "sqlite":
        wanted = _without_forward_foreign_keys(wanted, available=existing | {t.name for t in wanted})
    # sort_tables orders by foreign-key dependency, so parents are created first.
    #
    # Table by table, not metadata.create_all(tables=...): a metadata-level create
    # fires every Postgres enum type registered on the metadata, whichever tables are
    # named, so it made all of the ORM's enums here, including ones like
    # `curriculumstatus` that a7b8c9d0e1f2 then failed to CREATE TYPE again. A
    # table-level create makes only the enums that table uses.
    for table in sa.schema.sort_tables(wanted):
        table.create(bind=bind, checkfirst=True)


def _as_of_this_revision() -> sa.MetaData:
    """A copy of the ORM metadata minus ``LATER_COLUMNS`` (and their keys and indexes)."""
    scratch = sa.MetaData()
    for table in Base.metadata.sorted_tables:  # a key's target must resolve in the same MetaData
        skip = set(LATER_COLUMNS.get(table.name, ()))
        if not skip:
            table.to_metadata(scratch)
            continue
        # Column._copy() leaves out keys and unique constraints already bound to the table.
        constraints: list[sa.Constraint] = []
        for fkc in table.foreign_key_constraints:
            names = [c.name for c in fkc.columns]
            if not skip & set(names):
                constraints.append(
                    sa.ForeignKeyConstraint(
                        names,
                        [fk.target_fullname for fk in fkc.elements],
                        name=fkc.name,
                        ondelete=fkc.ondelete,
                        onupdate=fkc.onupdate,
                    )
                )
        for uc in table.constraints:
            names = [c.name for c in uc.columns]
            if isinstance(uc, sa.UniqueConstraint) and not skip & set(names):
                constraints.append(sa.UniqueConstraint(*names, name=uc.name))
        copy = sa.Table(
            table.name, scratch, *[c._copy() for c in table.columns if c.name not in skip], *constraints
        )
        have = {ix.name for ix in copy.indexes}
        for ix in table.indexes:
            names = [c.name for c in ix.columns]
            if ix.name not in have and not skip & set(names):
                sa.Index(ix.name, *[copy.c[n] for n in names], unique=ix.unique)
    return scratch


def _without_forward_foreign_keys(tables: list[sa.Table], available: set[str]) -> list[sa.Table]:
    """``tables`` (scratch copies) minus any foreign key to a table that does not exist yet.

    Building from the live ORM means taking whatever keys the models have *now*, and
    the models have since grown three whose targets a later revision creates:
    courses.qualification_id, course_modules.po_id and
    competency_auto_assessments.quiz_attempt_id. Postgres refuses a FOREIGN KEY to a
    missing table, so `upgrade head` on an empty database died here, at CREATE TABLE
    courses. SQLite does not check the target at CREATE time, which is why the chain
    looked fine there and is left as it was.

    The later revisions see the column already present and skip adding it, so the
    keys dropped here are added at the head of the chain by e6f7a8b9c0d1.
    """
    copies = tables  # already copies (``_as_of_this_revision``): edited in place
    for table in copies:
        for fkc in list(table.foreign_key_constraints):
            if fkc.elements[0].target_fullname.split(".")[0] in available:
                continue
            table.constraints.discard(fkc)
            for fk in fkc.elements:
                fk.parent.foreign_keys.discard(fk)
                table.foreign_keys.discard(fk)
    return copies


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    present = [
        Base.metadata.tables[name]
        for name in ORM_ONLY_TABLES
        if name in Base.metadata.tables and name in existing
    ]
    if not present:
        return
    # Reverse dependency order, so children go before their parents.
    for table in reversed(sa.schema.sort_tables(present)):
        table.drop(bind=bind, checkfirst=True)
