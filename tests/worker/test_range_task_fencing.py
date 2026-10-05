"""Range provision/destroy tasks act only for the operation the API recorded.

A task can arrive more than once (Celery redelivers after a worker loss; the API's
outbox re-sends when it cannot tell whether a send landed) or late (after the range
has moved on). The API records an operation by moving the range into ``provisioning``
or ``destroying`` (app/range_ops.py), so a task proceeds only while the range is still
in that state, and writes its outcome only from it. A duplicate or stale delivery
changes nothing and touches no hypervisor.

Retries are not stale deliveries: a failed attempt that will be retried leaves the
range in its in-progress state, so the retry proceeds; only the last attempt records
``failed``.

These run the real tasks against a SQLite database (the worker's raw SQL), with the
mock provisioner.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import sqlalchemy as sa  # noqa: E402
from worker import tasks  # noqa: E402


def _sqlite_now(dbapi_conn, _record):
    """The worker's SQL is written for PostgreSQL (NOW()); give SQLite one."""
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    yield from _db(tmp_path, monkeypatch)
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)


def _db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE templates (id TEXT PRIMARY KEY, yaml TEXT)"))
        conn.execute(
            sa.text(
                "CREATE TABLE ranges (id TEXT PRIMARY KEY, template_id TEXT, state TEXT, provisioner_backend TEXT, "
                "provisioner_output TEXT, error_message TEXT, updated_at TIMESTAMP)"
            )
        )
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield engine


def _range(db, state: str, output: str | None = None) -> str:
    rid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates VALUES (:id, :yaml)"), {"id": tid, "yaml": "id: t\n"})
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, provisioner_output) "
                "VALUES (:id, :t, :s, 'mock', :o)"
            ),
            {"id": rid, "t": tid, "s": state, "o": output},
        )
    return rid


def _state(db, rid: str) -> tuple[str, str | None]:
    with db.connect() as conn:
        return tuple(conn.execute(sa.text("SELECT state, error_message FROM ranges WHERE id = :id"), {"id": rid}).one())


def test_a_recorded_provision_runs_and_reaches_ready(db):
    rid = _range(db, "provisioning")
    assert tasks.provision_range.run(rid)["status"] == "ready"
    assert _state(db, rid)[0] == "ready"


@pytest.mark.parametrize("state", ["ready", "destroying", "destroyed", "created"])
def test_a_duplicate_or_stale_provision_changes_nothing(db, state):
    rid = _range(db, state)
    with patch.object(tasks, "_get_backend") as backend:
        result = tasks.provision_range.run(rid)
    assert result["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state


@pytest.mark.parametrize("state", ["destroyed", "ready", "provisioning"])
def test_a_duplicate_or_stale_destroy_changes_nothing(db, state):
    rid = _range(db, state, output='{"vms": []}')
    with patch.object(tasks, "_get_backend") as backend:
        result = tasks.destroy_range.run(rid)
    assert result["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state


def test_a_failed_attempt_that_will_be_retried_leaves_the_range_in_progress(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter busy")),
        patch.object(tasks, "_last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)
    assert _state(db, rid) == ("provisioning", None), "the retry must still find the range in provisioning"
    assert tasks.provision_range.run(rid)["status"] == "ready", "and then it proceeds"


def test_the_last_failed_attempt_records_failed(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter refused the clone")),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)  # called directly: no retry follows
    assert _state(db, rid) == ("failed", "vCenter refused the clone")


def test_a_destroy_reaches_destroyed_and_a_redelivery_is_skipped(db):
    rid = _range(db, "destroying", output='{"vms": []}')
    assert tasks.destroy_range.run(rid)["status"] == "destroyed"
    assert tasks.destroy_range.run(rid)["status"] == "skipped"
    assert _state(db, rid)[0] == "destroyed"
