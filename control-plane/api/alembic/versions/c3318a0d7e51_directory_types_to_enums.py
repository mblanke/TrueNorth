"""map legacy OU and security-group types onto the API's enums

Revision ID: c3318a0d7e51
Revises: 3e8b1c7a9d42
Create Date: 2026-10-28 09:00:00.000000

The Users page offered OU types "department" and "team" and the group type "role", which the
API refused on create. Rows created before the page was fixed (#318) or by an older API can
carry them, and editing such a row sends the old value back, which is now refused. They become
"custom" and "role_based". Other values are untouched. Data only; no schema change, and no
downgrade of the values (the old spellings were never valid).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3318a0d7e51"
down_revision: str | None = "3e8b1c7a9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "organizational_units" in tables:
        bind.execute(sa.text("UPDATE organizational_units SET ou_type = 'custom' "
                             "WHERE ou_type IN ('department', 'team')"))
    if "security_groups" in tables:
        bind.execute(sa.text("UPDATE security_groups SET group_type = 'role_based' WHERE group_type = 'role'"))


def downgrade() -> None:
    pass
