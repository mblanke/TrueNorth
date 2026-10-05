"""Two overlapping deliveries of one range task: only one reaches the hypervisor.

Independent review of the candidate (2026-10-05): the worker's claim was a conditional
update from the in-progress state to itself, so a second copy arriving while the first
was still running passed it too. Two builds ran; the second's output overwrote the
first's and orphaned its VMs. A copy arrives that way when a build outlives the broker's
visibility timeout (acks_late) or when two API processes re-send one operation.

Now a task also takes a lease on the range (worker/fencing.py): one holder at a time,
released when the task ends (so a retry after a failed attempt can claim it), and
expiring, so a copy redelivered after its worker died can take over.

Written to run on the code before the fix too: the lease table is created only if the
worker knows it.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import sqlalchemy as sa  # noqa: E402
from worker import tables, tasks  # noqa: E402
from worker.provisioners.base import DestroyResult, ProvisionResult, StopResult  # noqa: E402

OUTPUT = '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}'


def _sqlite_now(dbapi_conn, _record):
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
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
    tables.network_reservations.create(engine)
    if hasattr(tables, "range_leases"):
        tables.range_leases.create(engine)
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield engine
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)


def _range(db, state: str, output: str | None = None) -> str:
    rid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates VALUES (:id, 'id: t\n')"), {"id": tid})
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, provisioner_output) "
                "VALUES (:id, :t, :s, 'mock', :o)"
            ),
            {"id": rid, "t": tid, "s": state, "o": output},
        )
    return rid


class SlowBackend:
    """A hypervisor call that blocks until released, and counts how often it was made."""

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    async def _hold(self):
        self.calls += 1
        self.started.set()
        await asyncio.to_thread(self.release.wait, 10)

    async def provision(self, range_id, template, allocations):
        await self._hold()
        return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}], networks=[], duration_seconds=0)

    async def destroy(self, range_id, output):
        await self._hold()
        return DestroyResult(status="ok", resources_removed=1, duration_seconds=0, errors=[])

    async def stop(self, range_id, output):
        await self._hold()
        return StopResult(status="ok", vms_stopped=1, duration_seconds=0, errors=[])


@pytest.mark.parametrize(
    ("task", "state", "output"),
    [
        ("provision_range", "provisioning", None),
        ("destroy_range", "destroying", OUTPUT),
        ("stop_range", "stopping", OUTPUT),
    ],
)
def test_a_second_copy_while_the_first_runs_does_nothing(db, task, state, output):
    rid = _range(db, state, output)
    backend = SlowBackend()
    results: dict = {}
    with (
        patch.object(tasks, "_get_backend", return_value=backend),
        patch.object(tasks, "reserve_for_build", return_value={}),
    ):
        first = threading.Thread(target=lambda: results.update(first=getattr(tasks, task).run(rid)))
        first.start()
        assert backend.started.wait(10), "the first copy never reached the hypervisor"
        results["second"] = getattr(tasks, task).run(rid)  # arrives while the first runs
        backend.release.set()
        first.join(20)
    assert backend.calls == 1, f"{backend.calls} {task} calls reached the hypervisor"
    assert results["second"]["status"] == "skipped"
    assert results["first"]["status"] != "skipped"


def test_a_retry_after_a_failed_attempt_still_runs(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter busy")),
        patch.object(tasks, "_last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)
    assert tasks.provision_range.run(rid)["status"] == "ready", "the lease was released for the retry"


def test_a_lease_left_by_a_dead_worker_expires(db):
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            tables.range_leases.insert().values(
                range_id=rid, holder="dead-worker", expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_the_task_time_limit_ends_a_task_before_the_broker_redelivers_it():
    from worker.celery_app import app

    visibility = app.conf.broker_transport_options["visibility_timeout"]
    assert app.conf.task_time_limit and app.conf.task_time_limit < visibility
    assert int(os.environ.get("VSPHERE_PROVISION_BUDGET", "3300")) < app.conf.task_time_limit
