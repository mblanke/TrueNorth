"""7c4d2e9a1b60: when a roll-call Student was really notified; no "ready" status (#319).

Upgrade from its parent onto a database that already holds a roll call: ``class_roll_call``
gains ``notified_at`` and ``in_app_notified_at``, empty (nothing recorded whether old emails
went out, so those seats are kept rather than forfeited on a guess), and a ``ready`` entry
becomes ``signed_in``. Downgrade drops the two columns; re-upgrade restores them. SQLite here;
the PostgreSQL chain runs in CI (test_migration_populated_upgrade.py and
test_migration_downgrades.py with TEST_POSTGRES_ADMIN_URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "3e8b1c7a9d42", "7c4d2e9a1b60"
COLUMNS = {"notified_at", "in_app_notified_at"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("class_roll_call")}


def test_the_revision_chains_onto_the_previous_head():
    text = (API / f"alembic/versions/{REVISION}_roll_call_notified.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_columns_and_retires_ready(tmp_path):
    url = f"sqlite:///{tmp_path / 'notified.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    tenant, course, lead, cls = (uuid.uuid4() for _ in range(4))
    from app.classes.models import Class
    from app.models import Course, Tenant, User, UserRole
    from sqlalchemy.orm import Session

    with Session(engine) as db:  # tables this revision leaves alone: their ORM is the parent's schema
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, keycloak_id="kc", email="l@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today(), lead_instructor_id=lead, status="open"))
        db.commit()
    with engine.begin() as db:  # class_roll_call gains columns: written in SQL, as the parent had it
        for email, status in (("ann@corp.test", "invited"), ("bob@corp.test", "ready"), ("cat@corp.test", "signed_in")):
            db.execute(sa.text("INSERT INTO class_roll_call (id, class_id, tenant_id, email, status) "
                               "VALUES (:i, :c, :t, :e, :s)"),
                       {"i": str(uuid.uuid4()), "c": str(cls), "t": str(tenant), "e": email, "s": status})
        # Reminded under the older rule: that reminder was sent, so it counts as notice.
        db.execute(sa.text("UPDATE class_roll_call SET reminded_at = '2026-10-20 08:00:00' "
                           "WHERE email = 'ann@corp.test'"))
    assert not COLUMNS & _columns(engine)

    _alembic(url, "upgrade", REVISION)
    assert _columns(engine) >= COLUMNS
    with engine.connect() as db:
        rows = db.execute(sa.text(
            "SELECT email, status, notified_at, in_app_notified_at FROM class_roll_call ORDER BY email")).all()
        assert [tuple(r) for r in rows] == [("ann@corp.test", "invited", "2026-10-20 08:00:00", None),
                                            ("bob@corp.test", "signed_in", None, None),
                                            ("cat@corp.test", "signed_in", None, None)]

    _alembic(url, "downgrade", PARENT)
    assert not COLUMNS & _columns(engine)
    _alembic(url, "upgrade", REVISION)
    assert _columns(engine) >= COLUMNS
