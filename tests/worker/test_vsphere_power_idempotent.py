"""Powering a vSphere VM to the state it is already in is success, not an error.

vCenter answers ``POST /api/vcenter/vm/{id}/power?action=stop`` on a powered-off VM with
400 ``ALREADY_IN_DESIRED_STATE``. Stop and start are retried (a partial result, a
redelivery after a worker died, a student who shut a VM down from inside), so treating
that as a failure turned every retry into a failure and put a range whose VMs were off
back to ``ready`` (S3d review).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from worker.provisioners.vsphere_api import VsphereAPIProvisioner

ALREADY = {
    "error_type": "ALREADY_IN_DESIRED_STATE",
    "messages": [{"default_message": "Virtual machine is already powered off."}],
}


def _call(status: int, body: dict, action: str = "stop") -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/vcenter/vm/vm-7/power" and request.url.params["action"] == action
        return httpx.Response(status, json=body)

    prov = VsphereAPIProvisioner.__new__(VsphereAPIProvisioner)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://vc") as client:
            await prov._power_action(client, "vm-7", action)

    asyncio.run(run())


@pytest.mark.parametrize("action", ["stop", "start"])
def test_already_in_the_desired_state_is_success(action):
    _call(400, ALREADY, action)


def test_other_errors_still_fail():
    with pytest.raises(httpx.HTTPStatusError):
        _call(400, {"error_type": "INVALID_ARGUMENT", "messages": []})
    with pytest.raises(httpx.HTTPStatusError):
        _call(503, {"error_type": "SERVICE_UNAVAILABLE"})


def test_a_normal_power_action_succeeds():
    _call(204, {})
