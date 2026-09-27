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


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    wanted = [
        Base.metadata.tables[name]
        for name in ORM_ONLY_TABLES
        if name in Base.metadata.tables and name not in existing
    ]
    if not wanted:
        return
    # sort_tables orders by foreign-key dependency, so parents are created first.
    Base.metadata.create_all(bind=bind, tables=sa.schema.sort_tables(wanted), checkfirst=True)


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
