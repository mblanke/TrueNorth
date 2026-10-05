"""Powering a vSphere VM to the state it is already in is success, not an error.

Stop and start are retried (a partial result, a redelivery after a worker died, a
student who shut a VM down from inside). ``_power_action`` reads the VM's power state
first and leaves it alone if it is already there (#33); if it gets there between that
read and the call, vCenter answers 400 ``ALREADY_IN_DESIRED_STATE``, which is success too
(#30). Any other error still fails.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from worker.provisioners.vsphere_api import VsphereAPIProvisioner

ALREADY = {"error_type": "ALREADY_IN_DESIRED_STATE", "messages": []}


def _call(action: str, state: str, post_status: int = 204, post_body: dict | None = None) -> list[str]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/vcenter/vm/vm-7/power"
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"state": state})
        assert request.url.params["action"] == action
        return httpx.Response(post_status, json=post_body or {})

    prov = VsphereAPIProvisioner.__new__(VsphereAPIProvisioner)

    async def session() -> str:  # no login: the handler stands in for vCenter
        return "session-token"

    prov._get_session = session

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://vc") as client:
            await prov._power_action(client, "vm-7", action)

    asyncio.run(run())
    return seen


@pytest.mark.parametrize(("action", "state"), [("stop", "POWERED_OFF"), ("start", "POWERED_ON")])
def test_a_vm_already_there_is_left_alone(action, state):
    assert _call(action, state) == ["GET"]


@pytest.mark.parametrize(("action", "state"), [("stop", "POWERED_ON"), ("start", "POWERED_OFF")])
def test_getting_there_between_check_and_call_is_success(action, state):
    assert _call(action, state, 400, ALREADY) == ["GET", "POST"]


def test_other_errors_still_fail():
    with pytest.raises(httpx.HTTPStatusError):
        _call("stop", "POWERED_ON", 400, {"error_type": "INVALID_ARGUMENT"})
    with pytest.raises(httpx.HTTPStatusError):
        _call("start", "POWERED_OFF", 503, {"error_type": "SERVICE_UNAVAILABLE"})


def test_a_normal_power_action_is_sent():
    assert _call("stop", "POWERED_ON") == ["GET", "POST"]
