"""roll call: when a Student was really notified; no "ready" status

Revision ID: 7c4d2e9a1b60
Revises: 3e8b1c7a9d42
Create Date: 2026-10-27 22:00:00.000000

GA gaps B (issue #319; ADR 0014 §2, §4):

* ``class_roll_call.notified_at``: when the onboarding or day-before email was accepted by
  the SMTP relay. A seat is forfeited only once the Student was actually notified (owner
  direction). Rows that already have a day-before ``reminded_at`` get ``notified_at`` from it:
  that reminder was sent under the older rule, and without the backfill those Students would
  stay "never notified" for ever. Other existing rows stay empty (nothing recorded whether
  their onboarding email went out), so their seat is kept until a lead removes them.
* ``class_roll_call.in_app_notified_at``: when an in-app notification about the roll call
  was issued to the linked account (it counts once that account has signed in since).
* The roll-call status ``ready`` is dropped: ADR 0014 never defines it and nothing wrote it.
  Any row that has it becomes ``signed_in`` (status is a plain string, no type change).

Downgrade drops the two columns. ``ready`` is not restored: no row had it for a reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c4d2e9a1b60"
down_revision: str | None = "3e8b1c7a9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("notified_at", "in_app_notified_at")


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("class_roll_call")}


def upgrade() -> None:
    have = _columns()
    for name in COLUMNS:
        if name not in have:
            op.add_column("class_roll_call", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE class_roll_call SET status = 'signed_in' WHERE status = 'ready'"))
    bind.execute(sa.text(
        "UPDATE class_roll_call SET notified_at = reminded_at WHERE reminded_at IS NOT NULL AND notified_at IS NULL"
    ))


def downgrade() -> None:
    have = _columns()
    drop = [name for name in COLUMNS if name in have]
    if drop:
        with op.batch_alter_table("class_roll_call") as batch:
            for name in drop:
                batch.drop_column(name)
