"""Noise agents' management addresses are reserved, not derived from template order (S4b).

Every noise-enabled range puts its agents on one shared management portgroup. The
addresses used to be ``.10 + the agent's index in the template``, computed separately by
the worker (the NIC) and the API (the agent registration), so any two noisy ranges
collided and two from the same template collided exactly. Now provisioning reserves them
(app/network_inventory/) in the transaction that accepts the provision, the worker is
handed the reserved addresses, and deploy registers agents at those same addresses.
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from app import celery_client
from app.models import Range, RangeState
from app.network_inventory import NetworkReservation
from app.range_ops import service as range_ops

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


@pytest.fixture
def worker_takes_addresses(monkeypatch):
    """The provision contract once provision_range accepts ``noise_mgmt`` (an optional
    second argument). Until worker/tasks.py does, the contract parity test
    (tests/contracts) keeps the argument out, and the API sends only the range id."""
    from app import task_contracts as tc

    current = tc.TASKS["provision_range"]
    extra = tc.Arg("noise_mgmt", "object", required=False, description="{agent node: reserved address}")
    monkeypatch.setitem(
        tc.TASKS, "provision_range", tc.TaskContract(current.name, current.queue, (*current.args, extra))
    )


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


def test_the_worker_is_handed_the_reserved_addresses(client, db_session, sent, worker_takes_addresses):
    rid = _range(client, _template(client, _noisy()))
    client.post(f"/ranges/{rid}/provision")
    assert sent == [("provision_range", rid, _reserved(db_session, rid))]


def test_addresses_are_reserved_but_not_sent_while_the_worker_cannot_take_them(client, db_session, sent):
    """Today's contract: provision_range takes only the range id. The reservation still
    happens (deploy registers agents at it); a send with an extra argument would fail
    contract validation, and the worker would refuse it."""
    from app.task_contracts import validate_args

    rid = _range(client, _template(client, _noisy()))
    assert client.post(f"/ranges/{rid}/provision").status_code == 202
    assert _reserved(db_session, rid)
    assert sent == [("provision_range", rid)]
    validate_args("provision_range", (rid,))


def test_a_range_without_noise_reserves_nothing_and_sends_only_its_id(client, db_session, sent):
    rid = _range(client, _template(client, RVB))
    client.post(f"/ranges/{rid}/provision")
    assert sent == [("provision_range", rid)]
    assert _reserved(db_session, rid) == {}


def test_a_redispatch_sends_the_same_addresses(client, db_session, monkeypatch, worker_takes_addresses):
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


def test_running_an_exercise_on_a_noisy_range_reserves_like_a_provision(
    client, db_session, sent, worker_takes_addresses
):
    """POST /exercises/{id}/run provisions the range itself: it must reserve too."""
    rid = _range(client, _template(client, _noisy()))
    sid = client.post(
        "/scenarios", json={"name": "S", "version": "1.0", "yaml": "id: s\ntimeline: []", "is_public": True}
    ).json()["id"]
    ex = client.post("/exercises", json={"name": "E", "range_id": rid, "scenario_id": sid, "max_score": 10}).json()
    assert client.post(f"/exercises/{ex['id']}/run").status_code == 200
    held = _reserved(db_session, rid)
    assert held and ("provision_range", rid, held) in sent
    assert [o["action"] for o in client.get(f"/ranges/{rid}/operations").json()] == ["provision"]


def test_reprovisioning_after_a_template_change_follows_the_new_mgmt_network(client, db_session, sent):
    tid = _template(client, _noisy())
    rid = _range(client, tid)
    client.post(f"/ranges/{rid}/provision")
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state = RangeState.failed
    changed = _noisy(cidr="10.66.0.0/24")
    for node in changed["nodes"]:  # and one agent fewer
        if node["id"] == "lnx02":
            node["noise"] = {"agent": False}
    rng.template.yaml = yaml.safe_dump(changed)
    db_session.commit()
    assert client.post(f"/ranges/{rid}/provision").status_code == 202
    held = _reserved(db_session, rid)
    assert "lnx02" not in held, "a node that is no longer an agent keeps no address"
    assert held and all(v.startswith("10.66.0.") for v in held.values()), held


def test_turning_noise_off_releases_the_ranges_addresses(client, db_session, sent):
    rid = _range(client, _template(client, _noisy()))
    client.post(f"/ranges/{rid}/provision")
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state = RangeState.failed
    rng.template.yaml = yaml.safe_dump(RVB)
    db_session.commit()
    client.post(f"/ranges/{rid}/provision")
    assert _reserved(db_session, rid) == {}
    assert sent[-1] == ("provision_range", rid)


def test_overlapping_mgmt_networks_on_one_vlan_never_share_an_address(client, db_session, sent):
    """One portgroup per VLAN: a /24 and a /25 of it are the same wire."""
    a = _range(client, _template(client, _noisy(cidr="10.255.0.0/24")))
    b = _range(client, _template(client, _noisy(cidr="10.255.0.0/25")))
    client.post(f"/ranges/{a}/provision")
    client.post(f"/ranges/{b}/provision")
    assert not set(_reserved(db_session, a).values()) & set(_reserved(db_session, b).values())


def test_a_batch_that_overflows_the_pool_accepts_none(client, db_session, sent):
    tid = _template(client, _noisy(cidr=SMALL))  # room for three ranges
    ids = [_range(client, tid) for _ in range(4)]
    r = client.post("/ranges/batch-provision", json={"range_ids": ids})
    assert r.status_code == 409, r.text
    db_session.expire_all()
    assert db_session.query(NetworkReservation).count() == 0
    assert all(client.get(f"/ranges/{i}/operations").json() == [] for i in ids)
    assert sent == []


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


def test_the_api_sends_addresses_exactly_when_the_worker_contract_takes_them():
    """The send follows the published contract, which the worker's signature pins
    (tests/contracts/test_task_contracts.py): no flag to forget when the worker changes."""
    from app.range_ops import service
    from worker import contracts as worker_contracts

    takes = any(a.name == "noise_mgmt" for a in worker_contracts.TASKS["provision_range"].args)
    assert service._contract_takes("provision_range", "noise_mgmt") is takes


def test_on_postgres_concurrent_provisions_of_noisy_ranges_never_share_an_address():
    """Two provisions accepted at once from one template: the second waits for the
    first's domain lock, then takes the next free addresses."""
    import os
    import threading

    import sqlalchemy as sa
    from app.auth import CurrentUser
    from app.db import Base  # every section's tables: registered by app.main (conftest)
    from app.models import Template, Tenant, UserRole
    from sqlalchemy.orm import sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_noise_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=10)
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(engine, expire_on_commit=False)
        tenant = uuid.uuid4()
        with make() as s:
            s.add(Tenant(id=tenant, name="t", slug=f"t-{tenant.hex[:8]}"))
            s.flush()
            tpl = Template(id=uuid.uuid4(), name="n", yaml=yaml.safe_dump(_noisy()), tenant_id=tenant)
            s.add(tpl)
            s.flush()
            ids = [uuid.uuid4(), uuid.uuid4()]
            for rid in ids:
                s.add(Range(id=rid, name="r", template_id=tpl.id, tenant_id=tenant, state=RangeState.created))
            s.commit()
        who = CurrentUser(
            id=str(uuid.uuid4()),
            email="i@x",
            display_name="i",
            role=UserRole.admin,
            tenant_id=str(tenant),
            keycloak_id="kc",
        )
        sa_, sb = make(), make()
        range_ops.accept(sa_, ids[0], who, "provision")
        out: dict = {}
        t = threading.Thread(target=lambda: out.update(b=range_ops.accept(sb, ids[1], who, "provision")))
        t.start()
        t.join(0.5)
        assert "b" not in out, "the second acceptance did not wait"
        sa_.commit()
        t.join(10)
        sb.commit()
        with make() as s:
            rows = s.query(NetworkReservation).filter(NetworkReservation.kind == "noise_mgmt_ip").all()
            by_range = {rid: {r.value for r in rows if r.range_id == rid} for rid in ids}
        assert by_range[ids[0]] and len(by_range[ids[0]]) == len(by_range[ids[1]])
        assert not by_range[ids[0]] & by_range[ids[1]]
        sa_.close()
        sb.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
