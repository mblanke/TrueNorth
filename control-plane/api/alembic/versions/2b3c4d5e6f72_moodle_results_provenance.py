"""moodle results: the release and Moodle attempt each recorded fact came from

Revision ID: 2b3c4d5e6f72
Revises: a1e2b3c4d5f6
Create Date: 2026-10-12 10:00:00.000000

``moodle_result_records`` already matches a fact on immutable ids (platform, TrueNorth user
id, course id, activity idnumber). This records two more for provenance (M1):
``release_id``, the course release whose publication created the Moodle activity, and
``source_attempt_id``, Moodle's own id of the latest finished quiz attempt behind a grade
(``quiz_attempts.id``; null for completions and for plugins that do not report it).
Additive and nullable: existing rows keep null until Moodle reports the fact again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "2b3c4d5e6f72"
down_revision: str | None = "a1e2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "moodle_result_records"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    have = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if "release_id" not in have:
            batch.add_column(
                sa.Column(
                    "release_id",
                    GUID(),
                    sa.ForeignKey("course_releases.id", name="moodle_result_records_release_id_fkey"),
                    nullable=True,
                )
            )
        if "source_attempt_id" not in have:
            batch.add_column(sa.Column("source_attempt_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    have = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if "source_attempt_id" in have:
            batch.drop_column("source_attempt_id")
        if "release_id" in have:
            batch.drop_constraint("moodle_result_records_release_id_fkey", type_="foreignkey")
            batch.drop_column("release_id")
