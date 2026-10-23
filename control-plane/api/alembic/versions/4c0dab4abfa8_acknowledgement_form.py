"""acknowledgement form: versions and signatures

Revision ID: 4c0dab4abfa8
Revises: c1465c4964a6
Create Date: 2026-10-23 12:00:00.000000

Classes 2 (ADR 0014 decision 3; issue #279), ``app/acknowledgement``:

* ``acknowledgement_versions``: the rules-of-use text (Markdown), platform-wide. A draft
  until published; publishing numbers it and freezes it.
* ``acknowledgement_signatures``: who signed which version, when, in which tenant, as which
  email, name and role, from which address and browser.

Additive. One unpublished draft (the shipped default text) is added when the table is
empty, so nobody is asked to sign until an administrator publishes a version. Downgrade
drops both tables.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "4c0dab4abfa8"
down_revision: str | None = "c1465c4964a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy of app/acknowledgement/default_text.py as of this revision, so later edits to the
# shipped text never change what this migration seeds.
SEED_TEXT = """\
# Rules of use

TrueNorth Range is a training system. Before you use it, read these rules and agree to
them. You will be asked again whenever they change, and once a year.

## Use it for training only

- Use your own account. Do not share your sign-in or let anyone else use your session.
- Use the ranges, labs and tools only for the course, lab or exercise you are taking part in.
- Attack only the systems your instructor has put in scope. Never point range tools at
  anything outside the range.
- Do not try to get around the system's access controls, or reach another person's range,
  results or records.
- Do not copy malware, tools, credentials or course material out of the range unless your
  instructor tells you to.
- Report anything that looks wrong (a range you should not be able to reach, data that is
  not yours, a security problem) to your instructor or an administrator straight away.

## You are monitored

- What you do in TrueNorth is logged: sign-ins, the pages and records you open, the
  actions you take and the traffic in your range.
- These records are used to run and assess training, to keep the system secure and to
  investigate misuse.

## Sessions may be observed

- Instructors, assistant instructors and observers may watch your sessions while they
  happen, **including your live screens and consoles**, and may review them afterwards.
- Assume that anything you do or type in a range can be seen.

## Your records

- Your results, attendance and this agreement are kept as part of your training record.
- Your organisation's own policies on acceptable use of IT systems apply here as well.

Breaking these rules may end your access and be reported to your chain of command or
organisation.

By selecting **I agree** you confirm that you have read and understood these rules and
will follow them.
"""


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "acknowledgement_versions" not in existing:
        versions = op.create_table(
            "acknowledgement_versions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("version", sa.Integer(), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("version", name="uq_acknowledgement_versions_version"),
        )
        op.bulk_insert(versions, [{"id": uuid.uuid4(), "version": None, "text": SEED_TEXT}])
    if "acknowledgement_signatures" not in existing:
        op.create_table(
            "acknowledgement_signatures",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("version_id", GUID(), sa.ForeignKey("acknowledgement_versions.id"), nullable=False),
            sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("email", sa.String(255), nullable=False, server_default=""),
            sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("role", sa.String(32), nullable=False, server_default=""),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(255), nullable=True),
        )
        op.create_index("ix_ack_signatures_user_version", "acknowledgement_signatures",
                        ["user_id", "version_id", "signed_at"])
        op.create_index("ix_ack_signatures_tenant_signed", "acknowledgement_signatures", ["tenant_id", "signed_at"])


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "acknowledgement_signatures" in existing:
        op.drop_index("ix_ack_signatures_tenant_signed", table_name="acknowledgement_signatures")
        op.drop_index("ix_ack_signatures_user_version", table_name="acknowledgement_signatures")
        op.drop_table("acknowledgement_signatures")
    if "acknowledgement_versions" in existing:
        op.drop_table("acknowledgement_versions")
