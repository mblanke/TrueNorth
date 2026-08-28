"""add trainee registration requests + user onboarding state

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-25 12:00:00.000000

Identity comes from Active Directory via Keycloak, but authenticating proved
only who someone is — there was no way for them to *become* a TrueNorth user.
`get_current_user` returned a flat 403 to every valid token with no matching
row, and nothing anywhere created one.

`registration_requests` is the missing step: an AD-authenticated person submits
what AD does not hold (rank, unit, callsign, the qualification they are
joining), and an instructor approves it. Approval is the only path that creates
a trainee, and it is what assigns role and tenant.

Deliberately a separate table rather than a half-formed `users` row:
`users.tenant_id` is NOT NULL and `email`/`keycloak_id` are unique, so a pending
person could only live there behind a placeholder tenant that every one of the
~46 tenant-scoped lookups would then have to exclude. Keeping them out also
makes enforcement structural — `get_current_user` already refuses anyone
without a row, so a newly written router cannot forget a check that does not
exist.

The three `users` columns track the post-approval first-run flow.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Tolerant of a database Base.metadata.create_all has already touched: the
    # API calls it on every boot, so in dev these objects can exist before this
    # migration ever runs and a bare create_table would abort the upgrade.
    for col, kwargs in (
        ("onboarding_state", {"nullable": False, "server_default": "not_started"}),
        ("onboarded_at", {"nullable": True}),
        ("onboarding_data", {"nullable": False, "server_default": "{}"}),
    ):
        if not _has_column("users", col):
            type_ = sa.DateTime(timezone=True) if col == "onboarded_at" else (
                sa.String(20) if col == "onboarding_state" else sa.Text()
            )
            with op.batch_alter_table("users") as batch:
                batch.add_column(sa.Column(col, type_, **kwargs))

    if _has_table("registration_requests"):
        return

    op.create_table(
        "registration_requests",
        sa.Column("id", GUID(), primary_key=True),
        # -- Identity, from the validated token --------------------------------
        sa.Column("keycloak_id", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("ad_object_guid", sa.String(36), nullable=True),
        sa.Column("ad_distinguished_name", sa.String(512), nullable=True),
        sa.Column("ad_groups", sa.Text(), nullable=False, server_default="[]"),
        # -- What AD does not hold ---------------------------------------------
        sa.Column("rank", sa.String(50), nullable=True),
        sa.Column("service_branch", sa.String(100), nullable=True),
        sa.Column("unit", sa.String(255), nullable=True),
        sa.Column("callsign", sa.String(50), nullable=True),
        sa.Column("nation_id", GUID(), sa.ForeignKey("nations.id"), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="UTC"),
        # -- What they are joining ---------------------------------------------
        sa.Column(
            "requested_qualification_id", GUID(), sa.ForeignKey("qualifications.id"), nullable=True
        ),
        sa.Column(
            "requested_learning_path_id", GUID(), sa.ForeignKey("learning_paths.id"), nullable=True
        ),
        sa.Column("requested_cohort", sa.String(120), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        # -- Advisory only: derived from AD groups, never grants anything -------
        sa.Column("suggested_role", sa.String(20), nullable=True),
        sa.Column("suggested_tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        # -- Decision -----------------------------------------------------------
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "withdrawn", name="registrationstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("decided_by_user_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_user_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_regreq_status", "registration_requests", ["status"])
    op.create_index(
        "ix_regreq_suggested_tenant_status",
        "registration_requests",
        ["suggested_tenant_id", "status"],
    )
    op.create_index("ix_regreq_keycloak_id", "registration_requests", ["keycloak_id"])
    op.create_index("ix_regreq_email", "registration_requests", ["email"])
    # At most one OPEN request per identity, enforced by the database rather than
    # by a check-then-insert that two concurrent submissions could race.
    # Rejected/withdrawn rows remain for the audit trail and allow resubmission.
    op.create_index(
        "uq_regreq_open_per_identity",
        "registration_requests",
        ["keycloak_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    if _has_table("registration_requests"):
        op.drop_table("registration_requests")
        # Postgres keeps the enum type behind after the table is dropped.
        sa.Enum(name="registrationstatus").drop(op.get_bind(), checkfirst=True)

    for col in ("onboarding_data", "onboarded_at", "onboarding_state"):
        if _has_column("users", col):
            with op.batch_alter_table("users") as batch:
                batch.drop_column(col)
