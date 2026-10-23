"""classification-safe result transfer outbox and Secret ledger

Revision ID: 4d8f7a1c2b90
Revises: 32270311dba6
Create Date: 2026-10-23 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import CHAR, TypeDecorator

revision: str = "4d8f7a1c2b90"
down_revision: str | None = "32270311dba6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GUID(TypeDecorator):
    """Frozen copy of the UUID storage type used when this migration was authored."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))


def upgrade() -> None:
    op.create_table(
        "result_transfer_outbox",
        sa.Column("package_id", GUID(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("opaque_subject_id", GUID(), nullable=False),
        sa.Column("course_release_id", GUID(), nullable=False),
        sa.Column("assessment_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_id", GUID(), nullable=False),
        sa.Column("outcome", sa.String(length=4), nullable=False),
        sa.Column("source_enclave", sa.String(length=100), nullable=False),
        sa.Column("destination_enclave", sa.String(length=100), nullable=False),
        sa.Column("package_json", sa.Text(), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["course_release_id"], ["course_releases.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("package_id"),
    )
    op.create_index("ix_result_outbox_attempt", "result_transfer_outbox", ["attempt_id"])
    op.create_index("ix_result_outbox_tenant_state", "result_transfer_outbox", ["tenant_id", "state"])

    op.create_table(
        "result_subject_bindings",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("source_enclave", sa.String(length=100), nullable=False),
        sa.Column("opaque_subject_id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_enclave", "opaque_subject_id", name="uq_result_subject_source_token"),
    )
    op.create_index("ix_result_subject_user", "result_subject_bindings", ["tenant_id", "user_id"])

    op.create_table(
        "imported_results",
        sa.Column("package_id", GUID(), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("subject_binding_id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), nullable=False),
        sa.Column("source_enclave", sa.String(length=100), nullable=False),
        sa.Column("course_release_id", GUID(), nullable=False),
        sa.Column("assessment_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_id", GUID(), nullable=False),
        sa.Column("raw_score", sa.Integer(), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=4), nullable=False),
        sa.Column("scorer_version", sa.String(length=100), nullable=False),
        sa.Column("evidence_sha256_json", sa.Text(), nullable=False),
        sa.Column("supersedes_package_id", GUID(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["course_release_id"], ["course_releases.id"]),
        sa.ForeignKeyConstraint(["subject_binding_id"], ["result_subject_bindings.id"]),
        sa.ForeignKeyConstraint(["supersedes_package_id"], ["imported_results.package_id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("package_id"),
        sa.UniqueConstraint("supersedes_package_id", name="uq_imported_result_supersedes"),
    )
    op.create_index("ix_imported_results_attempt", "imported_results", ["source_enclave", "attempt_id"])
    op.create_index("ix_imported_results_user_time", "imported_results", ["tenant_id", "user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_imported_results_user_time", table_name="imported_results")
    op.drop_index("ix_imported_results_attempt", table_name="imported_results")
    op.drop_table("imported_results")
    op.drop_index("ix_result_subject_user", table_name="result_subject_bindings")
    op.drop_table("result_subject_bindings")
    op.drop_index("ix_result_outbox_tenant_state", table_name="result_transfer_outbox")
    op.drop_index("ix_result_outbox_attempt", table_name="result_transfer_outbox")
    op.drop_table("result_transfer_outbox")
