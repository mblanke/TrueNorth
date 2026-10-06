"""canonical objective validator names

Revision ID: d2f3a4b5c6d7
Revises: c1e2f3a4b5c6
Create Date: 2026-10-06 18:00:00.000000

Objective rows said ``validate.opensearch_query``, ``validate.opensearch.query`` and
``validate_opensearch_query`` (and the same for manual_ack / deliverable_check) for one
validator. Rewrite them to the canonical names in app/detections/names.py. Data only;
the downgrade leaves the canonical names, which every reader accepts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f3a4b5c6d7"
down_revision: str | None = "c1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPELLINGS = {
    "opensearch_query": ("validate.opensearch_query", "validate.opensearch.query", "validate_opensearch_query",
                         "opensearch.query"),
    "manual_ack": ("validate.manual_ack", "validate.manual.ack", "validate_manual_ack", "manual.ack"),
    "deliverable_check": ("validate.deliverable_check", "validate.deliverable.check", "validate_deliverable_check",
                          "deliverable.check"),
}


def upgrade() -> None:
    objectives = sa.table("objectives", sa.column("validator", sa.String))
    for canonical, old in _SPELLINGS.items():
        op.execute(objectives.update().where(objectives.c.validator.in_(old)).values(validator=canonical))


def downgrade() -> None:
    pass
