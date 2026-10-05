"""Shared-network addresses and VLANs: unique across tenants, idempotent, capacity-checked.

The noise engine (uncommitted, network-traffic-noise-tool worktree) gives each agent's
management NIC ``.10 + its index in the template`` on one portgroup shared by every
range, so any two noise-enabled ranges collide, and two from the same template collide
exactly. The vSphere branch allocates uplink IPs and VLANs under a Redis lock that is
skipped when Redis is down, with no database constraint. ``app/network_inventory.py``
is the replacement both adopt (S4b/S5a): these tests pin its contract.
"""

from __future__ import annotations

import uuid

import pytest
from app import network_inventory as inv
from app.models import Range, RangeState, Template, Tenant
from app.models_network import NetworkReservation
from sqlalchemy.exc import IntegrityError

DOMAIN = "vsphere:vcsa01:TN-Noise-Mgmt"
POOL = inv.parse_ip_pool("10.255.0.10-14")  # five addresses


def _range(db, tenant: uuid.UUID | None = None, state=RangeState.provisioning) -> Range:
    tenant = tenant or uuid.uuid4()
    if not db.get(Tenant, tenant):
        db.add(Tenant(id=tenant, name=f"t-{tenant.hex[:8]}", slug=f"t-{tenant.hex[:8]}"))
        db.flush()
    tmpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant)
    db.add(tmpl)
    db.flush()
    rng = Range(id=uuid.uuid4(), name="r", template_id=tmpl.id, tenant_id=tenant, state=state)
    db.add(rng)
    db.flush()
    return rng


def test_two_tenants_ranges_from_the_same_template_get_different_addresses(db_session):
    a, b = _range(db_session), _range(db_session)
    nodes = ["ws01", "ws02", "dc01"]
    got_a = inv.reserve(db_session, a, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=nodes)
    got_b = inv.reserve(db_session, b, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=nodes[:2])
    assert got_a == {"ws01": "10.255.0.10", "ws02": "10.255.0.11", "dc01": "10.255.0.12"}
    assert got_b == {"ws01": "10.255.0.13", "ws02": "10.255.0.14"}
    assert not set(got_a.values()) & set(got_b.values())


def test_reserving_again_returns_the_same_addresses(db_session):
    rng = _range(db_session)
    first = inv.reserve(db_session, rng, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01", "ws02"])
    again = inv.reserve(
        db_session, rng, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws02", "ws01", "ws03"]
    )
    assert again["ws01"] == first["ws01"] and again["ws02"] == first["ws02"]
    assert again["ws03"] == "10.255.0.12"
    assert db_session.query(NetworkReservation).count() == 3


def test_a_full_pool_reserves_nothing_and_says_why(db_session):
    a, b = _range(db_session), _range(db_session)
    inv.reserve(db_session, a, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["n1", "n2", "n3", "n4"])
    with pytest.raises(inv.PoolExhaustedError, match="has 1 free of 5; this range needs 2 more"):
        inv.reserve(db_session, b, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["x", "y"])
    assert db_session.query(NetworkReservation).filter_by(range_id=b.id).count() == 0


def test_the_same_address_in_another_domain_is_fine(db_session):
    a, b = _range(db_session), _range(db_session)
    got_a = inv.reserve(db_session, a, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01"])
    got_b = inv.reserve(
        db_session, b, domain="vsphere:vcsa02:TN-Noise-Mgmt", kind="noise_mgmt_ip", pool=POOL, holders=["ws01"]
    )
    assert got_a == got_b == {"ws01": "10.255.0.10"}


def test_the_database_refuses_a_duplicate_even_if_code_tried(db_session):
    a, b = _range(db_session), _range(db_session)
    inv.reserve(db_session, a, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01"])
    db_session.add(
        NetworkReservation(
            id=uuid.uuid4(),
            tenant_id=b.tenant_id,
            range_id=b.id,
            domain=DOMAIN,
            kind="noise_mgmt_ip",
            value="10.255.0.10",
            holder="ws01",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_destroyed_ranges_addresses_are_free_again(db_session):
    a = _range(db_session)
    inv.reserve(db_session, a, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["n1", "n2", "n3", "n4", "n5"])
    a.state = RangeState.destroyed
    db_session.flush()
    b = _range(db_session)
    got = inv.reserve(db_session, b, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01"])
    assert got == {"ws01": "10.255.0.10"}


def test_vlans_come_from_the_vlan_pool(db_session):
    a, b = _range(db_session), _range(db_session)
    pool = inv.vlan_pool("2000-2001,2005")
    assert inv.reserve(db_session, a, domain="vsphere:vcsa01:dvs1", kind="vlan", pool=pool, holders=["corp"]) == {
        "corp": "2000"
    }
    assert inv.reserve(
        db_session, b, domain="vsphere:vcsa01:dvs1", kind="vlan", pool=pool, holders=["corp", "dmz"]
    ) == {"corp": "2001", "dmz": "2005"}
    with pytest.raises(ValueError):
        inv.vlan_pool("4090-5000")


def test_pool_forms():
    assert inv.parse_ip_pool("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    assert inv.parse_ip_pool("10.30.32.100-102") == ["10.30.32.100", "10.30.32.101", "10.30.32.102"]
    assert inv.parse_ip_pool("10.1.1.5, 10.1.1.1-10.1.1.2") == ["10.1.1.1", "10.1.1.2", "10.1.1.5"]
    with pytest.raises(ValueError):
        inv.parse_ip_pool("10.0.0.9-10.0.0.1")


def test_a_successful_destroy_releases_the_ranges_reservations(client, db_session, monkeypatch):
    from app import celery_client

    monkeypatch.setattr(celery_client, "dispatch", lambda *a: "task-1")
    tmpl = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": "id: t\n", "is_public": True}).json()
    rng_json = client.post("/ranges", json={"name": "R", "template_id": tmpl["id"]}).json()
    rng = db_session.get(Range, uuid.UUID(rng_json["id"]))
    rng.state = RangeState.ready
    inv.reserve(db_session, rng, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01"])
    db_session.flush()
    assert [r["value"] for r in client.get(f"/ranges/{rng.id}/network-reservations").json()] == ["10.255.0.10"]
    assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
    rng.state = RangeState.destroyed  # the worker finished
    db_session.flush()
    client.get(f"/ranges/{rng.id}/operations")  # reconcile
    assert client.get(f"/ranges/{rng.id}/network-reservations").json() == []


def test_on_postgres_concurrent_reservations_in_one_domain_never_collide():
    """Two transactions reserving in one domain at once: the second waits, then takes the next free."""
    import os
    import threading

    import sqlalchemy as sa
    from app.sections import Base
    from sqlalchemy.orm import sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_net_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=10)
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(engine, expire_on_commit=False)
        with make() as s:
            a, b = _range(s), _range(s)
            s.commit()
        sa_, sb = make(), make()
        ra, rb = sa_.get(Range, a.id), sb.get(Range, b.id)
        got_a = inv.reserve(sa_, ra, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01", "ws02"])
        out: dict = {}
        t = threading.Thread(
            target=lambda: out.update(
                b=inv.reserve(sb, rb, domain=DOMAIN, kind="noise_mgmt_ip", pool=POOL, holders=["ws01", "ws02"])
            )
        )
        t.start()
        t.join(0.5)
        assert "b" not in out, "B did not wait for A's lock on the domain"
        sa_.commit()
        t.join(10)
        sb.commit()
        assert got_a == {"ws01": "10.255.0.10", "ws02": "10.255.0.11"}
        assert out["b"] == {"ws01": "10.255.0.12", "ws02": "10.255.0.13"}
        sa_.close()
        sb.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_another_tenant_cannot_see_a_ranges_reservations(client, db_session):
    from contextlib import contextmanager

    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app
    from app.models import UserRole

    tmpl = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": "id: t\n", "is_public": True}).json()
    rid = client.post("/ranges", json={"name": "R", "template_id": tmpl["id"]}).json()["id"]
    inv.reserve(
        db_session,
        db_session.get(Range, uuid.UUID(rid)),
        domain=DOMAIN,
        kind="noise_mgmt_ip",
        pool=POOL,
        holders=["ws01"],
    )
    db_session.flush()

    @contextmanager
    def acting_as(role, tenant):
        who = CurrentUser(
            id=str(uuid.uuid4()), email="x@x", display_name="x", role=role, tenant_id=tenant, keycloak_id="kc"
        )
        fastapi_app.dependency_overrides[get_current_user] = lambda: who
        try:
            yield
        finally:
            fastapi_app.dependency_overrides.pop(get_current_user, None)

    with acting_as(UserRole.admin, "00000000-0000-0000-0000-0000000000ff"):
        assert client.get(f"/ranges/{rid}/network-reservations").status_code == 404
    assert [r["value"] for r in client.get(f"/ranges/{rid}/network-reservations").json()] == ["10.255.0.10"]
