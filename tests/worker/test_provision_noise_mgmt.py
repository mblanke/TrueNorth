"""provision_range builds noise management NICs at the addresses the API reserved.

The API reserves each agent node's address when it accepts the provision
(app/noise/mgmt.py) and sends them as provision_range's optional ``noise_mgmt``
argument; the task hands them to render_topology, which derives none of its own
(tests/api/test_noise_reservations.py covers both ends of that).
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest

pytest.importorskip("celery")

from worker import tasks as worker_tasks  # noqa: E402

TEMPLATE = (
    "nodes:\n"
    "  - {id: lnx01, role: workstation, os: ubuntu-2404, vlan: corporate_lan, ip: 10.10.0.50}\n"
    "noise: {enabled: true}\n"
)


class _StopAfterRenderError(Exception):
    pass


@pytest.fixture
def rendered_with(monkeypatch):
    """Run provision_range's body up to render_topology; return what render was given."""
    seen: dict = {}

    def fake_render(template, range_id, resolver, noise_mgmt=None):
        seen["noise_mgmt"] = noise_mgmt
        raise _StopAfterRenderError

    @contextmanager
    def no_db():
        yield None

    monkeypatch.setattr("worker.render.render_topology", fake_render)
    monkeypatch.setattr("worker.render.golden_image_resolver", lambda db, hv: lambda alias: alias)
    monkeypatch.setattr(worker_tasks.db_ops, "range_template_and_backend", lambda db, rid: (TEMPLATE, "mock"))
    monkeypatch.setattr(worker_tasks.db_ops, "hypervisor_creds", lambda db, hv, range_id: {})
    monkeypatch.setattr(worker_tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(worker_tasks, "_update_range_state", lambda *a, **k: True)
    monkeypatch.setattr(worker_tasks, "_db_session", no_db)
    monkeypatch.setattr(worker_tasks, "_last_attempt", lambda task: False)
    body = inspect.unwrap(worker_tasks.provision_range.run)  # under @fenced: the task's own code

    def run(*args):
        with pytest.raises(_StopAfterRenderError):
            body(worker_tasks.provision_range, "r-1", *args)
        return seen["noise_mgmt"]

    return run


def test_the_reserved_addresses_reach_render(rendered_with):
    given = {"lnx01": "10.255.0.77"}
    assert rendered_with(given) == given


def test_a_range_sent_without_addresses_renders_without_them(rendered_with):
    assert rendered_with() is None
