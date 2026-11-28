# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Storage appliance & volume CRUD (app/routers/storage.py).

Tenant-owned infrastructure inventory. The behaviours worth pinning: an appliance with
volumes still on it refuses deletion with a 409, a volume on another tenant's appliance is
a 404 (it would carve space out of their storage), and the /summary roll-up totals.
"""

from __future__ import annotations

APPLIANCE = {
    "name": "netapp-01",
    "vendor": "NetApp",
    "model": "FAS2750",
    "management_ip": "10.0.4.10",
    "raw_capacity_tb": 100.0,
    "usable_capacity_tb": 80.0,
}


def _appliance(client, **overrides) -> dict:
    resp = client.post("/storage/appliances", json={**APPLIANCE, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _volume(client, appliance_id: str, **overrides) -> dict:
    resp = client.post(
        "/storage/volumes",
        json={"appliance_id": appliance_id, "volume_name": overrides.pop("volume_name", "vol0"), **overrides},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_appliance_create_list_patch(client, db_session):
    created = _appliance(client)
    assert created["protocol"] == "nfs"  # the schema default
    assert created["is_active"] is True

    listed = client.get("/storage/appliances")
    assert listed.status_code == 200, listed.text
    assert "netapp-01" in [a["name"] for a in listed.json()]

    patched = client.patch(f"/storage/appliances/{created['id']}", json={"usable_capacity_tb": 70.5})
    assert patched.status_code == 200, patched.text
    assert patched.json()["usable_capacity_tb"] == 70.5
    assert patched.json()["vendor"] == "NetApp"  # untouched field survives


def test_appliance_delete_blocked_while_volumes_remain_then_allowed(client, db_session):
    created = _appliance(client)
    appliance_id = created["id"]
    _volume(client, appliance_id, volume_name="blocker")

    refused = client.delete(f"/storage/appliances/{appliance_id}")
    assert refused.status_code == 409, refused.text
    assert "1 volume(s)" in refused.json()["detail"]

    volumes = client.get(f"/storage/volumes?appliance_id={appliance_id}")
    assert [v["volume_name"] for v in volumes.json()] == ["blocker"]
    assert client.delete(f"/storage/volumes/{volumes.json()[0]['id']}").status_code == 204

    assert client.delete(f"/storage/appliances/{appliance_id}").status_code == 204
    assert client.get("/storage/appliances").json() == []


def test_volume_on_foreign_appliance_is_404(client, db_session):
    from _shared import real_tenant
    from app.models import StorageAppliance

    other = real_tenant(db_session)
    foreign = StorageAppliance(
        name="their-array", vendor="Dell", model="ME4", management_ip="10.9.4.1", tenant_id=other.id
    )
    db_session.add(foreign)
    db_session.flush()

    resp = client.post("/storage/volumes", json={"appliance_id": str(foreign.id), "volume_name": "pirate"})
    assert resp.status_code == 404, resp.text
    assert "Appliance not found" in resp.json()["detail"]


def test_volume_filter_by_appliance_and_delete(client, db_session):
    a1 = _appliance(client, name="arr-1")
    a2 = _appliance(client, name="arr-2")
    _volume(client, a1["id"], volume_name="v-a1")
    _volume(client, a2["id"], volume_name="v-a2")

    only_a1 = client.get(f"/storage/volumes?appliance_id={a1['id']}")
    assert [v["volume_name"] for v in only_a1.json()] == ["v-a1"]

    all_volumes = client.get("/storage/volumes")
    assert len(all_volumes.json()) == 2

    vid = only_a1.json()[0]["id"]
    assert client.delete(f"/storage/volumes/{vid}").status_code == 204
    assert client.delete(f"/storage/volumes/{vid}").status_code == 404  # already gone


def test_summary_totals(client, db_session):
    assert client.get("/storage/summary").json() == {
        "total_appliances": 0,
        "active_appliances": 0,
        "total_raw_tb": 0,
        "total_usable_tb": 0,
        "total_volumes": 0,
    }

    a = _appliance(client, raw_capacity_tb=10.0, usable_capacity_tb=8.0)
    _volume(client, a["id"])
    idle = _appliance(client, name="idle", raw_capacity_tb=5.0, usable_capacity_tb=4.0)
    # StorageApplianceIn has no is_active: deactivating is a patch, not a create field.
    assert client.patch(f"/storage/appliances/{idle['id']}", json={"is_active": False}).status_code == 200

    summary = client.get("/storage/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json() == {
        "total_appliances": 2,
        "active_appliances": 1,
        "total_raw_tb": 15.0,
        "total_usable_tb": 12.0,
        "total_volumes": 1,
    }
