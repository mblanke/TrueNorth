# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Network device CRUD (app/routers/network_devices.py).

Physical network inventory scoped to the caller's tenant: create/list/patch/delete and
the /summary roll-up, which counts devices and groups them by role.
"""

from __future__ import annotations

import uuid

from app.models import NetworkDevice

BODY = {
    "name": "core-sw-01",
    "vendor": "Cisco",
    "model": "C9300",
    "role": "spine",
    "management_ip": "10.0.0.11",
    "firmware_version": "17.6.4",
    "port_count": 48,
}


def _create(client, **overrides) -> dict:
    resp = client.post("/network-devices/", json={**BODY, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_then_list(client, db_session):
    created = _create(client)
    assert created["name"] == "core-sw-01"
    assert created["role"] == "spine"
    assert created["is_active"] is True

    listed = client.get("/network-devices/")
    assert listed.status_code == 200, listed.text
    assert "core-sw-01" in [d["name"] for d in listed.json()]


def test_patch_updates_only_sent_fields(client, db_session):
    created = _create(client)
    device_id = created["id"]

    patched = client.patch(f"/network-devices/{device_id}", json={"firmware_version": "17.9.1", "port_count": 24})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["firmware_version"] == "17.9.1"
    assert body["port_count"] == 24
    assert body["name"] == "core-sw-01"  # untouched field survives
    assert body["management_ip"] == "10.0.0.11"


def test_patch_unknown_device_is_404(client, db_session):
    resp = client.patch(f"/network-devices/{uuid.uuid4()}", json={"port_count": 1})
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


def test_delete_unknown_device_is_404_and_known_is_204(client, db_session):
    assert client.delete(f"/network-devices/{uuid.uuid4()}").status_code == 404

    created = _create(client)
    assert client.delete(f"/network-devices/{created['id']}").status_code == 204
    listed = client.get("/network-devices/")
    assert created["name"] not in [d["name"] for d in listed.json()]


def test_summary_counts_devices_and_groups_by_role(client, db_session):
    _create(client, name="sw-1", role="spine")
    _create(client, name="sw-2", role="spine")
    _create(client, name="fw-1", role="firewall")
    _create(client, name="fw-2", role="firewall", port_count=0)

    summary = client.get("/network-devices/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["total_devices"] == 4
    assert body["active_devices"] == 4
    assert body["by_role"] == {"spine": 2, "firewall": 2}


def test_another_tenants_device_is_invisible_and_404(client, db_session):
    from _shared import real_tenant

    other = real_tenant(db_session)
    db_session.add(
        NetworkDevice(name="foreign-fw", vendor="Cisco", model="ASA", management_ip="10.9.9.9", tenant_id=other.id)
    )
    db_session.flush()

    listed = client.get("/network-devices/")
    assert listed.status_code == 200, listed.text
    assert "foreign-fw" not in [d["name"] for d in listed.json()]

    row = db_session.query(NetworkDevice).filter(NetworkDevice.name == "foreign-fw").one()
    assert client.delete(f"/network-devices/{row.id}").status_code == 404
