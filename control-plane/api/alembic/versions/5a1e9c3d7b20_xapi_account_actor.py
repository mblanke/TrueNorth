"""xAPI actor switches from email to an account IFI: record each user's legacy identity

Revision ID: 5a1e9c3d7b20
Revises: 1a2b3c4d5e63
Create Date: 2026-10-09 12:00:00.000000

A data migration, reversible. Until this revision every xAPI statement TrueNorth sent
named its actor by email (``mbox: mailto:<email>``); from now on the actor is an
``account`` IFI ``{homePage: XAPI_ACCOUNT_HOMEPAGE, name: users.id}`` (app/xapi.py).

TrueNorth's own database holds no statement bodies: statements go straight to the LRS,
and ``external_activities.xapi_statement_id`` is never written. So nothing in this
database is rewritten. What the switch loses is the join from the LRS's old statements
back to a person once their email changes, and this migration keeps it: one
``xapi_legacy_identities`` row per existing user (soft-deleted ones included: their
statements exist too), holding the ``mailto:`` IFI their statements carry, frozen now.
``python -m app.xapi_reissue`` reads it to re-issue that history under the new
actor when an operator chooses to; the LRS's own records are never rewritten here.

Upgrade creates and fills the table; it is idempotent (an existing row is kept).
Downgrade drops it: everything in it is derived from ``users`` at upgrade time, so a
downgrade followed by an upgrade rebuilds it (with today's emails).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "5a1e9c3d7b20"
down_revision: str | None = "1a2b3c4d5e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "xapi_legacy_identities"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        op.create_table(
            "xapi_legacy_identities",
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("legacy_mbox", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    users = sa.table("users", sa.column("id", GUID()), sa.column("email", sa.String()))
    legacy = sa.table(TABLE, sa.column("user_id", GUID()), sa.column("legacy_mbox", sa.Text()))
    already = sa.select(legacy.c.user_id).where(legacy.c.user_id == users.c.id)
    bind.execute(
        sa.insert(legacy).from_select(
            ["user_id", "legacy_mbox"],
            sa.select(users.c.id, sa.literal("mailto:") + users.c.email).where(
                users.c.email.is_not(None), users.c.email != "", ~sa.exists(already)
            ),
        )
    )


def downgrade() -> None:
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
