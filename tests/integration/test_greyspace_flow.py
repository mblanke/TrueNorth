"""Integration: attach a Greyspace block to a mock range through the live API (ADR 0007).

Runs in the CI ``integration`` job against the stack scripts/itest.sh starts (schema from
``alembic upgrade head``, so the range_greyspace migration is exercised too), then
provisions the range on the mock backend and waits for the worker's hook to record the
block as ``deployed``. The
simulated internet itself (DNS, routing, HTTP) is checked by the ``greyspace`` job
(greyspace/scripts/check-t0.sh), which needs Docker but not the API.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration


def test_attach_greyspace_to_a_mock_range(api_client, make_range):
    rid = make_range("greyspace-itest")

    before = api_client.get(f"/ranges/{rid}/greyspace")
    assert before.status_code == 200, before.text
    assert before.json()["status"] == "not_attached"

    corpora = {c["tier"]: c for c in api_client.get("/greyspace/corpora").json()}
    assert corpora["t0"]["available"] is True

    attached = api_client.put(f"/ranges/{rid}/greyspace", json={"site_packs": ["news", "search"]})
    assert attached.status_code == 200, attached.text
    body = attached.json()
    assert body["attached"] is True
    assert body["status"] == "configured"
    assert body["block"]["corpus_tier"] == "t0"
    assert body["problems"] == []

    status = api_client.get(f"/ranges/{rid}/greyspace").json()
    assert status["status"] == "configured" and status["block"]["site_packs"] == ["news", "search"]

    cfg = api_client.get(f"/ranges/{rid}/greyspace/config")
    assert cfg.status_code == 200, cfg.text
    cfg = cfg.json()
    assert {"dns-root", "dns-tld", "dns-auth", "resolver", "webfarm", "isp-a", "isp-b"} <= set(cfg["services"])
    assert cfg["site_packs"] == ["news", "search"]
    assert cfg["address_plan"]["resolver"]

    refused = api_client.put(f"/ranges/{rid}/greyspace", json={"site_packs": ["knitting"]})
    assert refused.status_code == 422

    # Provision on the mock backend: the worker's Greyspace hook records it as deployed.
    prov = api_client.post(f"/ranges/{rid}/provision")
    assert prov.status_code == 202, prov.text
    gs = _poll_greyspace(api_client, rid, "deployed")
    assert gs["range_state"] == "ready"
    assert gs["detail"]["backend"] == "mock" and "webfarm" in gs["detail"]["services"]
    assert gs["deployed_at"]

    assert api_client.delete(f"/ranges/{rid}/greyspace").status_code == 204
    assert api_client.get(f"/ranges/{rid}/greyspace").json()["status"] == "not_attached"
    api_client.post(f"/ranges/{rid}/destroy")  # so make_range's cleanup can delete it


def _poll_greyspace(client, rid: str, target: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/ranges/{rid}/greyspace").json()
        if last.get("status") == target:
            return last
        if last.get("range_state") == "failed":
            pytest.fail(f"range {rid} failed while waiting for Greyspace '{target}': {last}")
        time.sleep(2)
    raise TimeoutError(f"Greyspace on range {rid} did not reach '{target}' within {timeout}s (last: {last})")
