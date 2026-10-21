"""Range certification S1: a range template may not put a subnet on the site's own networks
(TN_RANGE_RESERVED_CIDRS, installer tn_range_reserved_cidrs).

Refused by /templates/validate (an error on the subnet's path), and by create and update
(422). The worker refuses it again at provision (tests/worker/test_range_cert_s1_vlans.py);
both copies of the rule must agree.
"""

from __future__ import annotations

import pytest
import yaml
from app.network_inventory import reserved as api_reserved
from worker import reserved_networks as worker_reserved

SITE = "10.30.30.0/24,10.30.31.0/24,10.30.32.0/24"


def _yaml(cidr: str) -> str:
    return yaml.safe_dump({"id": "t", "name": "t",
                           "network": {"vlans": [{"id": 10, "name": "lan", "cidr": cidr}]},
                           "nodes": [{"id": "a", "os": "ubuntu-2404", "vlan": "lan"}]})


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("TN_RANGE_RESERVED_CIDRS", SITE)


def test_validate_reports_a_subnet_on_a_site_network(client, site):
    body = client.post("/templates/validate", json={"yaml": _yaml("10.30.31.0/24")}).json()
    assert body["valid"] is False
    (err,) = [e for e in body["errors"] if e["path"] == "network.vlans.0.cidr"]
    assert "overlaps the reserved site network 10.30.31.0/24" in err["message"]
    assert client.post("/templates/validate", json={"yaml": _yaml("10.60.1.0/24")}).json()["valid"] is True


def test_create_and_update_refuse_it(client, site):
    made = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": _yaml("10.30.32.0/25")})
    assert made.status_code == 422 and "10.30.32.0/24" in made.json()["detail"]
    ok = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": _yaml("10.60.1.0/24")})
    assert ok.status_code in (200, 201), ok.text
    upd = client.put(f"/templates/{ok.json()['id']}", json={"yaml": _yaml("10.30.0.0/16")})
    assert upd.status_code == 422


def test_unset_means_no_site_networks(client, monkeypatch):
    monkeypatch.delenv("TN_RANGE_RESERVED_CIDRS", raising=False)
    assert client.post("/templates/validate", json={"yaml": _yaml("10.30.31.0/24")}).json()["valid"] is True


@pytest.mark.parametrize("cidr", ["10.30.30.0/24", "10.30.31.128/25", "10.0.0.0/8", "10.30.33.0/24", "bogus"])
def test_the_api_and_the_worker_agree(cidr):
    doc = yaml.safe_load(_yaml(cidr))
    api = bool(api_reserved.template_conflicts(doc, api_reserved.reserved_cidrs(SITE)))
    worker = bool(worker_reserved.conflicts(doc, worker_reserved.reserved_cidrs(SITE)))
    assert api == worker
