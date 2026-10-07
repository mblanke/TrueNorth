"""add scenario runs (inject records, scenario executions)

Revision ID: d8e9f0a1b2c3
Revises: f9a0b1c2d3e4
Create Date: 2026-10-07 18:00:00.000000

Exercises never fired an inject: the worker's dispatch seam was a no-op. Now each inject's
outcome is recorded in ``inject_records``, and a scenario can be run against a range
without an exercise (``scenario_executions``, POST /scenarios/execute). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "scenario_executions" not in existing:
        op.create_table(
            "scenario_executions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("scenario_id", GUID(), sa.ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True),
            sa.Column("scenario_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="SET NULL"), nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("definition", sa.JSON(), nullable=False),
            sa.Column("requested_by", GUID(), nullable=True),
            sa.Column("task_id", sa.String(255), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_scenario_executions_tenant", "scenario_executions", ["tenant_id"])
    if "inject_records" not in existing:
        op.create_table(
            "inject_records",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id", ondelete="CASCADE"), nullable=True),
            sa.Column(
                "execution_id", GUID(), sa.ForeignKey("scenario_executions.id", ondelete="CASCADE"), nullable=True
            ),
            sa.Column("range_id", GUID(), nullable=True),
            sa.Column("tenant_id", GUID(), nullable=True),
            sa.Column("run_id", sa.String(64), nullable=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="timeline"),
            sa.Column("seq", sa.Integer(), nullable=True),
            sa.Column("t", sa.String(16), nullable=True),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("execution_mode", sa.String(16), nullable=True),
            sa.Column("mitre_technique", sa.String(32), nullable=True),
            sa.Column("telemetry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("telemetry_shipped", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_inject_records_exercise", "inject_records", ["exercise_id"])
        op.create_index("ix_inject_records_execution", "inject_records", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_inject_records_execution", table_name="inject_records")
    op.drop_index("ix_inject_records_exercise", table_name="inject_records")
    op.drop_table("inject_records")
    op.drop_index("ix_scenario_executions_tenant", table_name="scenario_executions")
    op.drop_table("scenario_executions")
