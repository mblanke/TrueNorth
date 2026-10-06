"""Fixtures for worker task tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa


@pytest.fixture
def lease_always_free(monkeypatch):
    """For tests that stub the worker's database: the range's lease (worker/fencing.py)
    is always free and taking or giving it back touches nothing. Tests of the lease
    itself use a real database (test_range_task_fencing.py)."""
    from worker import fencing

    monkeypatch.setattr(fencing, "_claim_lease", lambda db, range_id, holder: True)
    monkeypatch.setattr(fencing, "_release_lease", lambda db, range_id, holder: None)
    monkeypatch.setattr(fencing, "_extend_lease", lambda db, range_id, holder: None)


# ── A real (SQLite) worker database, for the range-task tests ────────────


@pytest.fixture
def requeued(monkeypatch):
    """Every re-queue (fencing.defer) is recorded here; none reaches a broker."""
    from celery.app.task import Task

    calls: list[dict] = []
    monkeypatch.setattr(Task, "apply_async", lambda self, *a, **k: calls.append({"task": self.name, **k}))
    return calls


def _sqlite_now(dbapi_conn, _record):
    """The worker's SQL is written for PostgreSQL (NOW()); give SQLite one."""
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A file SQLite database with the worker's range tables, and the tasks pointed at it."""
    from worker import fencing, tasks

    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url, connect_args={"timeout": 30})
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE templates (id TEXT PRIMARY KEY, yaml TEXT)"))
        conn.execute(
            sa.text(
                "CREATE TABLE ranges (id TEXT PRIMARY KEY, template_id TEXT, state TEXT, provisioner_backend TEXT, "
                "provisioner_output TEXT, error_message TEXT, updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE range_snapshots (id TEXT PRIMARY KEY, range_id TEXT, snapshot_state TEXT, "
                "snapshot_data TEXT, range_state_at_snapshot TEXT, size_bytes INTEGER, updated_at TIMESTAMP)"
            )
        )
    fencing.range_leases.create(engine)
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield engine
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)
