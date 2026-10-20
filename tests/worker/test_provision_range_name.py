"""provision_range hands the provisioner the range's own name (``allocations["range_name"]``):
the vSphere provisioner names the range's VM folder and tn-range tag ``<name>-<id8>`` from it
(provisioners/vsphere_tags.py). The template's name is not the range's."""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest

pytest.importorskip("celery")

from worker import tasks as worker_tasks  # noqa: E402


class _StopAtProvisionError(Exception):
    pass


@pytest.fixture
def allocations_for(monkeypatch):
    seen: dict = {}

    @contextmanager
    def no_db():
        yield None

    async def provision(provisioner, range_id, template, allocations, session):
        seen.update(allocations=allocations, template=template)
        raise _StopAtProvisionError

    monkeypatch.setattr(worker_tasks.range_accounts, "provision", provision)
    monkeypatch.setattr(worker_tasks.range_alloc, "reserve_for_build", lambda *a, **k: {})
    monkeypatch.setattr(worker_tasks, "_get_backend", lambda *a, **k: object())
    monkeypatch.setattr(worker_tasks.greyspace, "plan_host", lambda range_id, backend, template: template)
    monkeypatch.setattr(worker_tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(worker_tasks, "_update_range_state", lambda *a, **k: True)
    monkeypatch.setattr(worker_tasks, "_db_session", no_db)
    monkeypatch.setattr(worker_tasks, "_last_attempt", lambda task: False)
    body = inspect.unwrap(worker_tasks.provision_range.run)

    def run(row):
        monkeypatch.setattr(worker_tasks.db_ops, "range_template_and_backend", lambda db, rid: row)
        with pytest.raises(_StopAtProvisionError):
            body(worker_tasks.provision_range, "r-1")
        return seen

    return run


def test_the_range_name_reaches_the_provisioner(allocations_for):
    seen = allocations_for(('{"name": "web-lab"}', "mock", "Blue Team 3"))
    assert seen["allocations"]["range_name"] == "Blue Team 3"
    assert seen["template"]["name"] == "web-lab"  # the template's own name is unchanged
    assert "finish_by" in seen["allocations"]


def test_a_row_without_the_name_still_provisions(allocations_for):
    seen = allocations_for(('{"name": "web-lab"}', "mock"))
    assert seen["allocations"]["range_name"] is None
