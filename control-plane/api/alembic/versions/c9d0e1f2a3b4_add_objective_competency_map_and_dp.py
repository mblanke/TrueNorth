"""add objective->competency map (NICE + NIST CSF) and developmental-progression fields

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-18 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) new competency framework value (Postgres enum needs ALTER TYPE; SQLite is VARCHAR)
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE competencyframework ADD VALUE IF NOT EXISTS 'nist_csf'")

    # 2) curated PO/EO -> competency mapping
    op.create_table(
        "objective_competency_map",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("po_id", GUID(), sa.ForeignKey("performance_objectives.id"), nullable=True),
        sa.Column("eo_id", GUID(), sa.ForeignKey("enabling_objectives.id"), nullable=True),
        sa.Column("competency_id", GUID(), sa.ForeignKey("competencies.id"), nullable=False),
        sa.Column("relation_type", sa.String(20), nullable=False, server_default="primary"),
        sa.Column("source", sa.String(40), nullable=False, server_default="curated"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ocm_po", "objective_competency_map", ["po_id"])
    op.create_index("ix_ocm_eo", "objective_competency_map", ["eo_id"])
    op.create_index("ix_ocm_comp", "objective_competency_map", ["competency_id"])

    # 3) developmental progression fields derived from the QSPs
    with op.batch_alter_table("qualifications") as batch:
        batch.add_column(sa.Column("dp_order", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("track", sa.String(20), nullable=False, server_default="progression"))
        batch.add_column(sa.Column("rank_level", sa.String(40), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("qualifications") as batch:
        batch.drop_column("rank_level")
        batch.drop_column("track")
        batch.drop_column("dp_order")
    op.drop_index("ix_ocm_comp", table_name="objective_competency_map")
    op.drop_index("ix_ocm_eo", table_name="objective_competency_map")
    op.drop_index("ix_ocm_po", table_name="objective_competency_map")
    op.drop_table("objective_competency_map")
    # NOTE: the 'nist_csf' enum value is intentionally left in place — Postgres cannot
    # drop an enum label without recreating the type; it is harmless if unused.
