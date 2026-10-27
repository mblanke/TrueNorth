"""class session ranges: individual or collective sessions, class ranges, session teams

Revision ID: 57e2c4b65b98
Revises: 8bc4d6fa0532
Create Date: 2026-10-23 18:00:00.000000

Classes 5 (ADR 0014 §9-11; issue #282):

* ``class_sessions.range_mode``: individual (a range per Student) or collective (one shared
  range). Existing sessions are collective, which is what they were: one booking, one range;
* ``class_course_range_modes``: a tenant's default mode for a course's class sessions;
* ``class_ranges``: the ranges a class owns (per seat, or shared), built at the lead before
  its first session that needs them and torn down after the last;
* ``class_session_teams``: a collective session's teams (ordinary ``teams`` rows).

Additive; no rows are written. Downgrade refuses while a class range still stands (its range
would be left with no owner to tear it down: cancel or close the class first), then drops
the three tables and the column. Teams the sessions made stay as ordinary teams. An individual
session becomes what the previous revision knows: one booking, one range from its template.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "57e2c4b65b98"
down_revision: str | None = "8bc4d6fa0532"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "range_mode" not in {c["name"] for c in insp.get_columns("class_sessions")}:
        op.add_column(
            "class_sessions",
            sa.Column("range_mode", sa.String(16), nullable=False, server_default="collective"),
        )
    if "class_course_range_modes" not in existing:
        op.create_table(
            "class_course_range_modes",
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), primary_key=True),
            sa.Column("course_id", GUID(), sa.ForeignKey("courses.id"), primary_key=True),
            sa.Column("mode", sa.String(16), nullable=False),
            sa.Column("updated_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            *_stamps(),
        )
    if "class_ranges" not in existing:
        op.create_table(
            "class_ranges",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("template_id", GUID(), sa.ForeignKey("templates.id"), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("entry_id", GUID(), sa.ForeignKey("class_roll_call.id"), nullable=True),
            sa.Column("seat_key", sa.String(64), nullable=False),
            sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id"), nullable=True),
            sa.Column("builds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("torn_down_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("teardown_reason", sa.String(64), nullable=True),
            sa.Column("last_error", sa.String(1000), nullable=True),
            *_stamps(),
            sa.UniqueConstraint("class_id", "template_id", "seat_key", name="uq_class_ranges_seat"),
        )
        op.create_index("ix_class_ranges_range", "class_ranges", ["range_id"])
        op.create_index("ix_class_ranges_tenant", "class_ranges", ["tenant_id"])
    if "class_session_teams" not in existing:
        op.create_table(
            "class_session_teams",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("session_id", GUID(), sa.ForeignKey("class_sessions.id"), nullable=False),
            sa.Column("team_id", GUID(), sa.ForeignKey("teams.id"), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            *_stamps(),
            sa.UniqueConstraint("team_id", name="uq_class_session_teams_team"),
        )
        op.create_index("ix_class_session_teams_session", "class_session_teams", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "class_ranges" in existing:
        standing = bind.execute(
            sa.text("SELECT count(*) FROM class_ranges WHERE range_id IS NOT NULL AND torn_down_at IS NULL")
        ).scalar()
        if standing:
            raise RuntimeError(
                f"{standing} class range(s) still stand; the previous revision has no owner to tear them down. "
                "Cancel their classes (or wait for their last session) and let them be torn down first"
            )
    if "class_session_teams" in existing:
        op.drop_index("ix_class_session_teams_session", table_name="class_session_teams")
        op.drop_table("class_session_teams")
    if "class_ranges" in existing:
        op.drop_index("ix_class_ranges_tenant", table_name="class_ranges")
        op.drop_index("ix_class_ranges_range", table_name="class_ranges")
        op.drop_table("class_ranges")
    if "class_course_range_modes" in existing:
        op.drop_table("class_course_range_modes")
    if "range_mode" in {c["name"] for c in insp.get_columns("class_sessions")}:
        with op.batch_alter_table("class_sessions") as batch:
            batch.drop_column("range_mode")
