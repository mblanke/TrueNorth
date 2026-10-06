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


# ── vSphere (from the adversarial review of 5dd2457) ────────────────────


class FakeVcenter:
    """Each VM's power state; the power action answers as vCenter does."""

    def __init__(self, states: dict[str, str], fail: set[str] = frozenset()):
        self.states, self.fail, self.actions = dict(states), set(fail), []

    async def get(self, client, path):
        return {"state": self.states[path.split("/")[3]]}

    async def act(self, client, vm_id, action):
        import httpx

        self.actions.append((vm_id, action))
        want = {"stop": "POWERED_OFF", "start": "POWERED_ON"}[action]
        if vm_id in self.fail or self.states[vm_id] == want:
            request = httpx.Request("POST", f"https://vc/api/vcenter/vm/{vm_id}/power")
            body = "ALREADY_IN_DESIRED_STATE" if self.states[vm_id] == want else "busy"
            raise httpx.HTTPStatusError(
                "400", request=request, response=httpx.Response(400, request=request, text=body)
            )
        self.states[vm_id] = want


def _vsphere(monkeypatch, vc: FakeVcenter):
    import asyncio
    from contextlib import asynccontextmanager

    from worker.provisioners.vsphere_api import VsphereAPIProvisioner

    prov = VsphereAPIProvisioner()

    async def session():
        return "s"

    @asynccontextmanager
    async def client(session):
        yield None

    monkeypatch.setattr(prov, "_get_session", session)
    monkeypatch.setattr(prov, "_client", client)
    monkeypatch.setattr(prov, "_api_get", vc.get)
    monkeypatch.setattr(prov, "_power_action", vc.act)
    return prov, asyncio.run


VMS = {"vms": [{"name": "dc01", "vm_id": "vm-1"}, {"name": "ws01", "vm_id": "vm-2"}]}


def test_a_vm_already_off_counts_as_stopped_so_a_retry_can_succeed(monkeypatch):
    """Attempt 1 stopped vm-1 and failed on vm-2; the retry must not fail on vm-1."""
    vc = FakeVcenter({"vm-1": "POWERED_OFF", "vm-2": "POWERED_ON"})
    prov, run = _vsphere(monkeypatch, vc)
    result = run(prov.stop("r", VMS))
    assert result.status == "ok" and result.vms_stopped == 2
    assert vc.actions == [("vm-2", "stop")]


def test_already_in_desired_state_from_vcenter_is_not_an_error(monkeypatch):
    """The VM got there between the read and the action."""
    vc = FakeVcenter({"vm-1": "POWERED_ON", "vm-2": "POWERED_ON"})
    prov, run = _vsphere(monkeypatch, vc)
    monkeypatch.setattr(prov, "_api_get", _stale_read(vc))
    assert run(prov.start("r", VMS)).status == "ok"


def _stale_read(vc):
    async def read(client, path):  # reports off; the VM is in fact already on
        return {"state": "POWERED_OFF"}

    return read


def test_a_real_failure_names_the_right_vm_and_a_vm_without_an_id_is_an_error(monkeypatch):
    vms = {"vms": [{"name": "dc01"}, {"name": "ws01", "vm_id": "vm-2"}, {"name": "db01", "vm_id": "vm-3"}]}
    vc = FakeVcenter({"vm-2": "POWERED_ON", "vm-3": "POWERED_ON"}, fail={"vm-3"})
    prov, run = _vsphere(monkeypatch, vc)
    result = run(prov.stop("r", vms))
    assert result.status == "partial" and result.vms_stopped == 1
    assert [e.split(":")[0] for e in result.errors] == ["VM dc01", "VM db01"]
