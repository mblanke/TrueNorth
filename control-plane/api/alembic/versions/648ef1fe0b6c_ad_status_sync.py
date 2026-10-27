"""AD status sync: its runs, the accounts they deactivated, and when accounts were last seen

Revision ID: 648ef1fe0b6c
Revises: 0fdeb8975b40
Create Date: 2026-10-27 12:00:00.000000

Identity 13 (ADR 0014 decision 6; issue #290):

* ``ad_status_sync_runs``: one row per run; ``slot`` is unique, which is how one replica
  claims the night's run;
* ``ad_status_sync_results``: the accounts a run deactivated (or, dry, would have);
* ``user_last_seen``: when TrueNorth last saw each account authenticate.

Additive; no rows are written. ``user_last_seen`` starts empty on purpose: an account with no
row counts from the first sync run (app/ad_status_sync/service.last_use), so nobody is
disabled for the time before TrueNorth kept the record. Downgrade drops the three tables;
accounts the sync deactivated stay deactivated, and their audit rows stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "648ef1fe0b6c"
down_revision: str | None = "0fdeb8975b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "ad_status_sync_runs" not in existing:
        op.create_table(
            "ad_status_sync_runs",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("slot", sa.String(64), nullable=False, unique=True),
            sa.Column("trigger", sa.String(16), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("triggered_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("dry_run", sa.Boolean(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("counts", sa.JSON(), nullable=False),
            sa.Column("error", sa.String(1000), nullable=True),
        )
        op.create_index("ix_ad_status_sync_runs_started", "ad_status_sync_runs", ["started_at"])
    if "ad_status_sync_results" not in existing:
        op.create_table(
            "ad_status_sync_results",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("run_id", GUID(), sa.ForeignKey("ad_status_sync_runs.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reason", sa.String(32), nullable=False),
            sa.Column("applied", sa.Boolean(), nullable=False),
            sa.Column("seats_freed", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ad_status_sync_results_run_tenant", "ad_status_sync_results", ["run_id", "tenant_id"])
    if "user_last_seen" not in existing:
        op.create_table(
            "user_last_seen",
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "user_last_seen" in existing:
        op.drop_table("user_last_seen")
    if "ad_status_sync_results" in existing:
        op.drop_index("ix_ad_status_sync_results_run_tenant", table_name="ad_status_sync_results")
        op.drop_table("ad_status_sync_results")
    if "ad_status_sync_runs" in existing:
        op.drop_index("ix_ad_status_sync_runs_started", table_name="ad_status_sync_runs")
        op.drop_table("ad_status_sync_runs")
