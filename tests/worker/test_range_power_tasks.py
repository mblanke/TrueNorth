"""Stopping and starting a range powers its VMs off and on (codereview1 S0 defect).

``/stop`` used to set ``stopped`` and send nothing: the VMs kept running on the hypervisor
while the platform said they were off. Now the API records a stop/start operation and
moves the range to ``stopping``/``starting`` (app/range_ops.py); these tasks power the
VMs through the range's provisioner and report what happened. Like provision and
destroy, a task acts only while the range is in the state its operation set, so a
duplicate or late delivery touches no hypervisor.

A failure leaves the range where its VMs most likely are: a stop that did not stop them
returns it to ``ready``, a start that did not start them to ``stopped``, each with the
error. The operation is then reconciled as failed.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import uuid  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from worker import power_tasks, tasks  # noqa: E402
from worker.provisioners.base import StartResult, StopResult  # noqa: E402


def _sqlite_now(dbapi_conn, _record):
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The worker's tables, in SQLite (as tests/worker/test_range_task_fencing.py)."""
    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
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
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)


def _range(db, state: str, output: str | None = None) -> str:
    rid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, provisioner_output) "
                "VALUES (:id, NULL, :s, 'mock', :o)"
            ),
            {"id": rid, "s": state, "o": output},
        )
    return rid


def _state(db, rid: str) -> tuple:
    with db.connect() as conn:
        return tuple(conn.execute(sa.text("SELECT state, error_message FROM ranges WHERE id = :id"), {"id": rid}).one())


OUTPUT = '{"vms": [{"vm_id": "vm-1", "name": "r-ws01"}, {"vm_id": "vm-2", "name": "r-dc01"}]}'


def _backend(stop=None, start=None):
    b = AsyncMock()
    b.stop.return_value = stop or StopResult(status="ok", vms_stopped=2, duration_seconds=0.1, errors=[])
    b.start.return_value = start or StartResult(status="ok", vms_started=2, duration_seconds=0.1, errors=[])
    return b


def test_a_stop_powers_the_vms_off_then_reads_stopped(db):
    rid = _range(db, "stopping", output=OUTPUT)
    backend = _backend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert power_tasks.stop_range.run(rid)["status"] == "stopped"
    backend.stop.assert_awaited_once()
    assert backend.stop.await_args.args[1]["vms"][0]["vm_id"] == "vm-1"
    assert _state(db, rid) == ("stopped", None)


def test_a_start_powers_the_vms_on_then_reads_ready(db):
    rid = _range(db, "starting", output=OUTPUT)
    backend = _backend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert power_tasks.start_range.run(rid)["status"] == "ready"
    backend.start.assert_awaited_once()
    assert _state(db, rid) == ("ready", None)


def test_the_mock_backend_end_to_end(db):
    rid = _range(db, "stopping", output=OUTPUT)
    assert power_tasks.stop_range.run(rid)["status"] == "stopped"
    tasks._update_range_state(rid, "starting")
    assert power_tasks.start_range.run(rid)["status"] == "ready"


@pytest.mark.parametrize("state", ["ready", "stopped", "destroying", "provisioning"])
def test_a_duplicate_or_stale_stop_touches_no_hypervisor(db, state):
    rid = _range(db, state, output=OUTPUT)
    with patch.object(tasks, "_get_backend") as backend:
        assert power_tasks.stop_range.run(rid)["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state


@pytest.mark.parametrize("state", ["ready", "stopped", "destroying"])
def test_a_duplicate_or_stale_start_touches_no_hypervisor(db, state):
    rid = _range(db, state, output=OUTPUT)
    with patch.object(tasks, "_get_backend") as backend:
        assert power_tasks.start_range.run(rid)["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state


def test_a_partial_stop_is_not_reported_as_stopped(db):
    rid = _range(db, "stopping", output=OUTPUT)
    partial = StopResult(status="partial", vms_stopped=1, duration_seconds=0.1, errors=["VM r-dc01: timed out"])
    with patch.object(tasks, "_get_backend", return_value=_backend(stop=partial)), pytest.raises(RuntimeError):
        power_tasks.stop_range.run(rid)
    state, error = _state(db, rid)
    assert state == "ready" and "r-dc01" in error


def test_a_failed_start_returns_the_range_to_stopped(db):
    rid = _range(db, "starting", output=OUTPUT)
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter unreachable")),
        pytest.raises(RuntimeError),
    ):
        power_tasks.start_range.run(rid)
    assert _state(db, rid) == ("stopped", "vCenter unreachable")


def test_a_failed_attempt_that_will_be_retried_stays_in_progress(db):
    rid = _range(db, "stopping", output=OUTPUT)
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("busy")),
        patch.object(power_tasks, "_last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        power_tasks.stop_range.run(rid)
    assert _state(db, rid) == ("stopping", None)


def test_a_success_clears_an_earlier_error(db):
    rid = _range(db, "stopping", output=OUTPUT)
    tasks._update_range_state(rid, "stopping", error="earlier stop failed")
    with patch.object(tasks, "_get_backend", return_value=_backend()):
        power_tasks.stop_range.run(rid)
    assert _state(db, rid) == ("stopped", None)
