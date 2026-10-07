"""Integration: attach a Greyspace block to a mock range through the live API (ADR 0007).

Runs in the CI ``integration`` job against the stack scripts/itest.sh starts (schema from
``alembic upgrade head``, so the range_greyspace migration is exercised too). The
simulated internet itself (DNS, routing, HTTP) is checked by the ``greyspace`` job
(greyspace/scripts/check-t0.sh), which needs Docker but not the API.
"""

from __future__ import annotations

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

    assert api_client.delete(f"/ranges/{rid}/greyspace").status_code == 204
    assert api_client.get(f"/ranges/{rid}/greyspace").json()["status"] == "not_attached"
