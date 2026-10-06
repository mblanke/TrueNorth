"""The worker's stop_range / start_range (worker/power_tasks.py; CR1-05): fenced on the
state the API recorded, and the outcome written only once the hypervisor has done it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_range_task_fencing import OUTPUT, _leases, _range, _state
from worker import power_tasks, tasks
from worker.provisioners.results import StartResult, StopResult


def _backend(status="ok"):
    b = MagicMock()
    b.stop = AsyncMock(
        return_value=StopResult(status=status, vms_stopped=1, errors=[] if status == "ok" else ["vm-1 busy"])
    )
    b.start = AsyncMock(return_value=StartResult(status=status, vms_started=1))
    return b


@pytest.mark.parametrize(
    ("task", "claim", "done", "call"),
    [
        (power_tasks.stop_range, "stopping", "stopped", "stop"),
        (power_tasks.start_range, "starting", "running", "start"),
    ],
)
def test_power_reaches_its_state_once_the_hypervisor_did_it(db, task, claim, done, call):
    rid = _range(db, claim, OUTPUT)
    backend = _backend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert task.run(rid)["status"] == done
    getattr(backend, call).assert_awaited_once()
    assert _state(db, rid)[0] == done and _leases(db, rid) == 0


@pytest.mark.parametrize("state", ["ready", "running", "stopped", "destroying"])
def test_a_duplicate_or_stale_stop_touches_nothing(db, state):
    rid = _range(db, state, OUTPUT)
    backend = _backend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert power_tasks.stop_range.run(rid)["status"] == "skipped"
    backend.stop.assert_not_awaited()
    assert _state(db, rid)[0] == state


def test_a_failed_stop_is_retried_and_only_the_last_attempt_records_failed(db):
    rid = _range(db, "stopping", OUTPUT)
    with (
        patch.object(tasks, "_get_backend", return_value=_backend("partial")),
        patch.object(power_tasks, "last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        power_tasks.stop_range.run(rid)
    assert _state(db, rid)[0] == "stopping", "a retry must still find it stopping"
    with patch.object(tasks, "_get_backend", return_value=_backend("partial")), pytest.raises(RuntimeError):
        power_tasks.stop_range.run(rid)  # called directly: the last attempt
    assert _state(db, rid) == ("failed", "stop partial: vm-1 busy")


def test_a_range_with_no_vms_recorded_fails_without_touching_the_hypervisor(db):
    rid = _range(db, "stopping", '{"vms": []}')
    with patch.object(tasks, "_get_backend") as backend, pytest.raises(RuntimeError, match="no VMs"):
        power_tasks.stop_range.run(rid)
    backend.assert_not_called()
    assert _state(db, rid)[0] == "failed"


def test_power_tasks_have_the_range_time_limits():
    from worker.celery_app import app

    for task in (power_tasks.stop_range, power_tasks.start_range):
        assert task.soft_time_limit and task.time_limit < app.conf.broker_transport_options["visibility_timeout"]
