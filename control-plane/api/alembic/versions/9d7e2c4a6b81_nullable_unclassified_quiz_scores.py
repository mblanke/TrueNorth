"""allow grade-free Unclassified quiz persistence

Revision ID: 9d7e2c4a6b81
Revises: 8bc4d6fa0532
Create Date: 2026-10-26 09:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d7e2c4a6b81"
down_revision: str | None = "8bc4d6fa0532"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("quiz_attempts") as batch:
        batch.alter_column("score", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("max_score", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("module_progress") as batch:
        batch.alter_column("score", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("max_score", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("enrollments") as batch:
        batch.alter_column("final_score", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("max_score", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute(sa.text("UPDATE quiz_attempts SET score = 0 WHERE score IS NULL"))
    op.execute(sa.text("UPDATE quiz_attempts SET max_score = 0 WHERE max_score IS NULL"))
    op.execute(sa.text("UPDATE module_progress SET score = 0 WHERE score IS NULL"))
    op.execute(sa.text("UPDATE module_progress SET max_score = 0 WHERE max_score IS NULL"))
    op.execute(sa.text("UPDATE enrollments SET final_score = 0 WHERE final_score IS NULL"))
    op.execute(sa.text("UPDATE enrollments SET max_score = 0 WHERE max_score IS NULL"))
    # module_progress is created by the historical ORM-reconciliation revision from
    # current metadata, so its parent schema is already nullable on fresh/round-trip
    # installs. Normalize values for old code, but preserve that parent schema.
    with op.batch_alter_table("quiz_attempts") as batch:
        batch.alter_column("max_score", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("score", existing_type=sa.Integer(), nullable=False)
