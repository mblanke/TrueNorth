"""Directory, auth-zone and storage CRUD against the live API.

Single-tenant only. The integration stack runs with AUTH_DISABLED=true, where every
request is the fixed dev admin in tenant 00000000-0000-0000-0000-000000000001; there is
no way to present a second tenant's identity over HTTP, so cross-tenant 404s are
covered in-process by tests/api/test_platform_tenancy.py. What this adds is the real
stack: PostgreSQL (not SQLite), the migrated schema, nginx/uvicorn routing, and that
rows created through the API carry the caller's tenant.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _ok(resp, *codes):
    assert resp.status_code in codes, f"{resp.request.method} {resp.request.url} => {resp.status_code} {resp.text}"
    return resp.json() if resp.content else None


def test_directory_ou_and_group_crud(api_client):
    s = _suffix()
    parent = _ok(api_client.post("/directory/ous", json={"name": f"itest-root-{s}", "slug": f"itest-root-{s}"}), 201)
    child = None
    group = None
    try:
        assert parent["tenant_id"] == DEV_TENANT
        child = _ok(
            api_client.post(
                "/directory/ous",
                json={"name": f"itest-child-{s}", "slug": f"itest-child-{s}", "parent_id": parent["id"]},
            ),
            201,
        )
        assert child["parent_id"] == parent["id"]

        tree = _ok(api_client.get("/directory/ous/tree"), 200)
        root = next(n for n in tree if n["id"] == parent["id"])
        assert [c["id"] for c in root["children"]] == [child["id"]]

        renamed = _ok(api_client.patch(f"/directory/ous/{child['id']}", json={"name": f"itest-renamed-{s}"}), 200)
        assert renamed["name"] == f"itest-renamed-{s}"

        group = _ok(
            api_client.post(
                "/directory/groups",
                json={"name": f"itest-g-{s}", "slug": f"itest-g-{s}", "ou_id": parent["id"]},
            ),
            201,
        )
        assert group["tenant_id"] == DEV_TENANT
        assert any(g["id"] == group["id"] for g in _ok(api_client.get("/directory/groups"), 200))
        _ok(api_client.patch(f"/directory/groups/{group['id']}", json={"description": "itest"}), 200)

        # An id that is not ours (here: not anyone's) is 404, not 403 or 500.
        assert api_client.get(f"/directory/ous/{uuid.uuid4()}").status_code == 404
        assert api_client.get(f"/directory/groups/{uuid.uuid4()}").status_code == 404
        bad_parent = api_client.post(
            "/directory/ous", json={"name": f"x-{s}", "slug": f"x-{s}", "parent_id": str(uuid.uuid4())}
        )
        assert bad_parent.status_code == 404
    finally:
        if group:
            api_client.delete(f"/directory/groups/{group['id']}")
        if child:
            api_client.delete(f"/directory/ous/{child['id']}")
        api_client.delete(f"/directory/ous/{parent['id']}")

    assert api_client.get(f"/directory/ous/{parent['id']}").status_code == 404


def test_auth_zone_crud(api_client):
    name = f"itest-zone-{_suffix()}"
    zone = _ok(api_client.post("/auth-zones", json={"zone_name": name}), 201)
    try:
        assert zone["tenant_id"] == DEV_TENANT
        assert any(z["id"] == zone["id"] for z in _ok(api_client.get("/auth-zones"), 200))
        assert api_client.post("/auth-zones", json={"zone_name": name}).status_code == 409
        updated = _ok(api_client.patch(f"/auth-zones/{zone['id']}", json={"zone_name": name, "require_mfa": False}), 200)
        assert updated["require_mfa"] is False
    finally:
        _ok(api_client.delete(f"/auth-zones/{zone['id']}"), 204)
    assert api_client.get(f"/auth-zones/{zone['id']}").status_code == 404


def test_storage_appliance_and_volume_crud(api_client):
    s = _suffix()
    before = _ok(api_client.get("/storage/summary"), 200)
    appliance = _ok(
        api_client.post(
            "/storage/appliances",
            json={
                "name": f"itest-array-{s}",
                "vendor": "Dell",
                "model": "PowerStore",
                "management_ip": "10.255.0.10",
                "raw_capacity_tb": 4,
                "usable_capacity_tb": 3,
            },
        ),
        201,
    )
    volume = None
    try:
        volume = _ok(
            api_client.post("/storage/volumes", json={"appliance_id": appliance["id"], "volume_name": f"itest-vol-{s}"}),
            201,
        )
        vols = _ok(api_client.get("/storage/volumes", params={"appliance_id": appliance["id"]}), 200)
        assert [v["id"] for v in vols] == [volume["id"]]

        patched = _ok(api_client.patch(f"/storage/appliances/{appliance['id']}", json={"is_active": False}), 200)
        assert patched["is_active"] is False

        after = _ok(api_client.get("/storage/summary"), 200)
        assert after["total_appliances"] == before["total_appliances"] + 1
        assert after["total_volumes"] == before["total_volumes"] + 1

        orphan = api_client.post("/storage/volumes", json={"appliance_id": str(uuid.uuid4()), "volume_name": "x"})
        assert orphan.status_code == 404
    finally:
        if volume:
            _ok(api_client.delete(f"/storage/volumes/{volume['id']}"), 204)
        _ok(api_client.delete(f"/storage/appliances/{appliance['id']}"), 204)
    assert api_client.patch(f"/storage/appliances/{appliance['id']}", json={"name": "gone"}).status_code == 404
