"""roll-call seat forfeit: forfeited_at and joined_at on class_roll_call

Revision ID: 32270311dba6
Revises: 5e6d8a033133
Create Date: 2026-10-23 18:00:00.000000

Classes 3 (ADR 0014 §4; issue #280):

* ``class_roll_call.forfeited_at``: when a Student lost their seat for not signing in by
  2 hours after the class's first approved session started. Kept after a reinstatement,
  so the clock never forfeits that entry twice;
* ``class_roll_call.joined_at``: when the entry was last put on the roll call, so someone
  added after the start gets their own 2 hours.

The roll-call status ``forfeited`` is a plain string, no type change. Additive; no rows are
written. Downgrade turns a ``forfeited`` entry into ``removed`` (the older code does not know
the status, and a forfeited Student is off the roll call with their seat already freed,
exactly as a removed one is), then drops the two columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "32270311dba6"
down_revision: str | None = "5e6d8a033133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("forfeited_at", "joined_at")


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("class_roll_call")}


def upgrade() -> None:
    have = _columns()
    for name in COLUMNS:
        if name not in have:
            op.add_column("class_roll_call", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE class_roll_call SET status = 'removed', removed_at = COALESCE(removed_at, forfeited_at) "
        "WHERE status = 'forfeited'"
    ))
    have = _columns()
    drop = [name for name in COLUMNS if name in have]
    if drop:
        with op.batch_alter_table("class_roll_call") as batch:
            for name in drop:
                batch.drop_column(name)
