# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Kit definition CRUD (app/routers/kit.py): infra inventory scoped to the caller's tenant.

Reads need ``infra:read`` and changes ``infra:write``; a foreign tenant's kit is a 404,
not a leak. The caller here is AUTH_DISABLED's dev admin (tests/conftest.py), who holds
both permissions.
"""

from __future__ import annotations

import uuid

from app.models import KitDefinition


def _body(slug: str) -> dict:
    return {"name": f"Range Kit {slug}", "slug": slug, "description": "rack + nodes + storage + network"}


def test_create_then_list_returns_the_tenant_own_kit(client, db_session):
    created = client.post("/kits/", json=_body("kit-create-list"))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"] == "kit-create-list"
    assert body["name"] == "Range Kit kit-create-list"

    listed = client.get("/kits/")
    assert listed.status_code == 200, listed.text
    slugs = [k["slug"] for k in listed.json()]
    assert "kit-create-list" in slugs


def test_create_persists_a_row_in_the_callers_tenant(client, db_session):
    from _shared import DEV_TENANT
    from sqlalchemy import select

    created = client.post("/kits/", json=_body("kit-persist"))
    assert created.status_code == 201, created.text
    row = db_session.scalar(select(KitDefinition).where(KitDefinition.slug == "kit-persist"))
    assert row is not None
    assert str(row.tenant_id) == DEV_TENANT


def test_delete_unknown_kit_is_404(client, db_session):
    assert client.delete(f"/kits/{uuid.uuid4()}").status_code == 404


def test_delete_removes_the_kit(client, db_session):
    created = client.post("/kits/", json=_body("kit-delete"))
    assert created.status_code == 201, created.text
    kit_id = created.json()["id"]

    deleted = client.delete(f"/kits/{kit_id}")
    assert deleted.status_code == 204, deleted.text

    from sqlalchemy import select

    assert db_session.scalar(select(KitDefinition).where(KitDefinition.id == uuid.UUID(kit_id))) is None
    assert deleted.content == b""


def test_another_tenants_kit_is_404_on_delete_and_invisible_on_list(client, db_session):
    from _shared import real_tenant

    other = real_tenant(db_session)
    db_session.add(KitDefinition(name="Foreign kit", slug="kit-foreign", tenant_id=other.id))
    db_session.flush()

    listed = client.get("/kits/")
    assert listed.status_code == 200, listed.text
    assert "kit-foreign" not in [k["slug"] for k in listed.json()]

    rows = db_session.query(KitDefinition).filter(KitDefinition.slug == "kit-foreign").all()
    assert len(rows) == 1
    assert client.delete(f"/kits/{rows[0].id}").status_code == 404
