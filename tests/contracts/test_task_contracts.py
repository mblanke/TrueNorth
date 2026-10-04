"""The API -> worker task contract (worker/worker/contracts.py, ADR 0002) matches both ends."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest import mock

import pytest
from app import celery_client, task_contracts
from worker import contracts as worker_contracts
from worker.celery_app import app as worker_app

ROOT = Path(__file__).resolve().parents[2]


def _worker_tasks() -> dict:
    return {n: t for n, t in worker_app.tasks.items() if n.startswith(worker_contracts.TASK_PREFIX)}


def test_api_copy_is_the_worker_contract():
    src = (ROOT / "control-plane/worker/worker/contracts.py").read_text()
    mirror = (ROOT / "control-plane/api/app/task_contracts.py").read_text()
    assert mirror.endswith(src), "run scripts/export_task_contracts.py"


def test_every_worker_task_has_a_contract_and_vice_versa():
    registered = {n.removeprefix(worker_contracts.TASK_PREFIX) for n in _worker_tasks()}
    assert registered == set(worker_contracts.TASKS)


@pytest.mark.parametrize("name", sorted(worker_contracts.TASKS))
def test_contract_args_match_the_task_signature(name):
    task = _worker_tasks()[worker_contracts.TASK_PREFIX + name]
    params = [p for p in inspect.signature(task.run).parameters.values() if p.name != "self"]
    contract = worker_contracts.TASKS[name]
    assert [p.name for p in params] == [a.name for a in contract.args]
    for p, a in zip(params, contract.args, strict=True):
        assert (p.default is inspect.Parameter.empty) == a.required, f"{name}.{a.name} optionality"


@pytest.mark.parametrize("name", sorted(worker_contracts.TASKS))
def test_sender_and_worker_route_every_task_to_the_same_queue(name):
    qualified = worker_contracts.TASK_PREFIX + name
    expected = worker_contracts.TASKS[name].queue
    assert celery_client.celery_app.amqp.router.route({}, qualified)["queue"].name == expected
    assert worker_app.amqp.router.route({}, qualified)["queue"].name == expected


def test_queues_are_ones_the_workers_consume():
    """compose runs worker-provision (provision,destroy), worker-scenario (scenario,default),
    worker-telemetry (telemetry). A contract naming any other queue would never run."""
    consumed = {"provision", "destroy", "scenario", "default", "telemetry"}
    assert {c.queue for c in worker_contracts.TASKS.values()} <= consumed


def test_dispatch_sends_contracted_args_to_the_contracted_queue():
    with mock.patch.object(celery_client.celery_app, "send_task") as send:
        send.return_value.id = "t-1"
        assert celery_client.dispatch("snapshot_range", "r-1", "s-1") == "t-1"
    send.assert_called_once_with("worker.tasks.snapshot_range", args=["r-1", "s-1"])


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("no_such_task", ()),
        ("provision_range", ()),  # missing arg
        ("provision_range", ("r", "extra")),  # too many
        ("batch_provision", ("not-a-list",)),  # wrong type
        ("run_scenario_v2", ("ex", ["not", "a", "dict"])),
    ],
)
def test_dispatch_rejects_calls_outside_the_contract(name, args):
    with (
        mock.patch.object(celery_client.celery_app, "send_task") as send,
        pytest.raises(task_contracts.TaskContractError),
    ):
        celery_client.dispatch(name, *args)
    send.assert_not_called()


def test_optional_arg_may_be_omitted():
    with mock.patch.object(celery_client.celery_app, "send_task") as send:
        celery_client.dispatch("generate_learning_recommendation", "u-1")
    send.assert_called_once_with("worker.tasks.generate_learning_recommendation", args=["u-1"])


def test_broker_outage_returns_none_instead_of_raising():
    with mock.patch.object(celery_client.celery_app, "send_task", side_effect=ConnectionError("down")):
        assert celery_client.dispatch("provision_range", "r-1") is None
