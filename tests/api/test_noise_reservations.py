"""Noise agents' management addresses are reserved, not derived from template order (S4b).

Every noise-enabled range puts its agents on one shared management portgroup. The
addresses used to be ``.10 + the agent's index in the template``, computed separately by
the worker (the NIC) and the API (the agent registration), so any two noisy ranges
collided and two from the same template collided exactly. Now provisioning reserves them
(app/network_inventory.py) in the transaction that accepts the provision, the worker is
handed the reserved addresses, and deploy registers agents at those same addresses.
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from app import celery_client, range_ops
from app.models import Range, RangeState
from app.models_network import NetworkReservation

REPO = Path(__file__).resolve().parents[2]
RVB = yaml.safe_load((REPO / "content/ranges/red-vs-blue/template.yaml").read_text())


def _noisy(**mgmt) -> dict:
    t = copy.deepcopy(RVB)
    t["noise"] = {"enabled": True, "mgmt": {"controller_url": "https://10.255.0.1/api", **mgmt}}
    return t


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    def send(task, *args):
        calls.append((task, *args))
        return f"task-{len(calls)}"

    monkeypatch.setattr(celery_client, "dispatch", send)
    return calls


def _template(client, template: dict) -> str:
    r = client.post(
        "/templates",
        json={
            "name": f"N {uuid.uuid4().hex[:6]}",
            "version": "1.0",
            "yaml": yaml.safe_dump(template),
            "is_public": True,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _range(client, template_id: str) -> str:
    r = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": template_id})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _reserved(db, range_id: str) -> dict[str, str]:
    db.expire_all()
    return {
        r.holder: r.value
        for r in db.query(NetworkReservation).filter(
            NetworkReservation.range_id == uuid.UUID(range_id), NetworkReservation.kind == "noise_mgmt_ip"
        )
    }


def test_two_ranges_from_one_noisy_template_get_different_mgmt_addresses(client, db_session, sent):
    tid = _template(client, _noisy())
    a, b = _range(client, tid), _range(client, tid)
    assert client.post(f"/ranges/{a}/provision").status_code == 202
    assert client.post(f"/ranges/{b}/provision").status_code == 202
    got_a, got_b = _reserved(db_session, a), _reserved(db_session, b)
    assert got_a and set(got_a) == set(got_b), "every agent node holds an address"
    assert not set(got_a.values()) & set(got_b.values()), (got_a, got_b)


def test_the_worker_is_handed_the_reserved_addresses(client, db_session, sent):
    rid = _range(client, _template(client, _noisy()))
    client.post(f"/ranges/{rid}/provision")
    assert sent == [("provision_range", rid, _reserved(db_session, rid))]


def test_a_range_without_noise_reserves_nothing_and_sends_only_its_id(client, db_session, sent):
    rid = _range(client, _template(client, RVB))
    client.post(f"/ranges/{rid}/provision")
    assert sent == [("provision_range", rid)]
    assert _reserved(db_session, rid) == {}


def test_a_redispatch_sends_the_same_addresses(client, db_session, monkeypatch):
    rid = _range(client, _template(client, _noisy()))
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: None)
    client.post(f"/ranges/{rid}/provision")
    calls = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: calls.append((task, *args)) or "t")
    assert range_ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
    assert calls == [("provision_range", rid, _reserved(db_session, rid))]


SMALL = "10.254.9.0/27"  # .10-.30: 21 addresses, three ranges of the template's 7 agents


def test_a_full_mgmt_pool_refuses_the_provision_and_records_nothing(client, db_session, sent):
    tid = _template(client, _noisy(cidr=SMALL))
    full = [_range(client, tid) for _ in range(3)]
    for rid in full:
        assert client.post(f"/ranges/{rid}/provision").status_code == 202
    assert sum(len(_reserved(db_session, rid)) for rid in full) == 21
    late = _range(client, tid)
    r = client.post(f"/ranges/{late}/provision")
    assert r.status_code == 409 and "pool" in r.json()["detail"], r.text
    db_session.expire_all()
    assert db_session.get(Range, uuid.UUID(late)).state != RangeState.provisioning
    assert client.get(f"/ranges/{late}/operations").json() == []
    assert _reserved(db_session, late) == {}
    assert len(sent) == 3


def test_destroying_a_range_frees_its_addresses_for_the_next(client, db_session, sent):
    tid = _template(client, _noisy(cidr=SMALL))
    full = [_range(client, tid) for _ in range(3)]
    for rid in full:
        client.post(f"/ranges/{rid}/provision")
    held = _reserved(db_session, full[0])
    rng = db_session.get(Range, uuid.UUID(full[0]))
    rng.state = RangeState.destroyed
    db_session.commit()
    late = _range(client, tid)
    assert client.post(f"/ranges/{late}/provision").status_code == 202
    assert set(_reserved(db_session, late).values()) == set(held.values())


# ── Deploy registers agents where the worker put them ──────────────────
@pytest.fixture
def deployed(monkeypatch):
    out = []
    monkeypatch.setattr("app.routers.noise._dispatch", lambda task, *args: out.append((task, args)) or "task-9")
    return out


def test_deploy_uses_the_reserved_addresses(client, db_session, sent, deployed):
    tid = _template(client, _noisy())
    first, rid = _range(client, tid), _range(client, tid)
    client.post(f"/ranges/{first}/provision")  # takes the low addresses
    client.post(f"/ranges/{rid}/provision")
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state = RangeState.ready
    db_session.commit()
    reserved = _reserved(db_session, rid)

    dry = client.post(f"/noise/ranges/{rid}/deploy", json={"dry_run": True}).json()
    assert {a["node"]: a["mgmt_ip"] for a in dry["agents"]} == {a["node"]: reserved[a["node"]] for a in dry["agents"]}
    r = client.post(f"/noise/ranges/{rid}/deploy", json={})
    assert r.status_code == 200, r.text
    ((_, (inventory,)),) = deployed
    assert inventory["agents"] and all(a["mgmt_ip"] == reserved[a["node"]] for a in inventory["agents"])


def test_deploy_refuses_a_range_provisioned_without_reservations(client, db_session, deployed):
    # Provisioned before S4b: its NICs have template-order addresses nobody holds.
    rid = _range(client, _template(client, _noisy()))
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state = RangeState.ready
    db_session.commit()
    r = client.post(f"/noise/ranges/{rid}/deploy", json={})
    assert r.status_code == 409 and "reprovision" in r.json()["detail"], r.text
    assert not deployed


# ── Worker side ─────────────────────────────────────────────────────────
def test_worker_builds_mgmt_nics_at_the_addresses_it_is_given():
    from worker.render import render_topology

    t = _noisy(vlan_id=4001, cidr="10.255.0.0/24")
    given = {"lnx01": "10.255.0.77", "tgen01": "10.255.0.78"}
    rendered = render_topology(t, "abcdef0123456789", lambda alias: alias, noise_mgmt=given)
    nics = {vm["name"][9:]: vm["mgmt"] for vm in rendered["vm_definitions"] if "mgmt" in vm}
    assert {h: n["ip"] for h, n in nics.items()} == given
    assert {n["prefix"] for n in nics.values()} == {24} and {n["vlan_id"] for n in nics.values()} == {4001}


def test_worker_derives_no_addresses_of_its_own():
    from worker.render import render_topology

    rendered = render_topology(_noisy(), "abcdef0123456789", lambda alias: alias)
    assert not any("mgmt" in vm for vm in rendered["vm_definitions"])
    assert not any(n["name"] == "noise_mgmt" for n in rendered["network_definitions"])


def test_the_provision_contract_takes_the_addresses():
    from worker.contracts import validate_args

    validate_args("provision_range", ("r1",))
    validate_args("provision_range", ("r1", {"lnx01": "10.255.0.10"}))
