# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
"""scripts/promote_golden_build_inprocess.py against the real golden-image build routes.

The stopgap for issue #527 (no Promote button yet): ``promote()`` goes through the API's own
promote route, so only the platform administrator can promote, the newest build is the one
promoted, the audit entry carries the reason, and an image whose build is already promoted
is left alone.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import AuditLog, GoldenImage, UserRole

ROOT = Path(__file__).resolve().parents[2]


def _script():
    path = ROOT / "scripts/promote_golden_build_inprocess.py"
    spec = importlib.util.spec_from_file_location("promote_golden_build_inprocess", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Api:
    def __init__(self, client):
        self.client = client

    def get(self, path, params=None, **kw):
        r = self.client.get(path, params=params)
        return r.status_code, (r.json() if r.content else None)

    def post(self, path, json=None, **kw):
        r = self.client.post(path, json=json)
        return r.status_code, (r.json() if r.content else None)


@pytest.fixture
def tenant(db_session, monkeypatch):
    """The operator's own tenant (PLATFORM_TENANT_ID): its admin is the platform administrator."""
    row = real_tenant(db_session, "promote-script")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(row.id))
    return row


@pytest.fixture
def image(client, db_session, tenant):
    img = GoldenImage(
        catalogue_id="srv2022", hypervisor="vsphere", template_name="srv2022", enabled=True, build_status="built"
    )
    db_session.add(img)
    db_session.commit()
    act_as(real_user(db_session, UserRole.admin, tenant.id))
    for build_id, checksum in (("srv2022-b1", "a" * 64), ("srv2022-b2", "b" * 64)):
        body = {
            "build": {
                "build_id": build_id,
                "checksum": checksum,
                "build_date": "2026-11-23",
                "iso_checksum": "",
                "copies": {"esx02": "srv2022@esx02-20261123"},
            }
        }
        assert client.patch(f"/golden-images/{img.id}", json=body).status_code == 200
    return img


def _promoted(client, image):
    return client.get(f"/golden-images/{image.id}/builds").json()["promoted_build_id"]


def test_the_platform_admin_promotes_the_newest_build_with_the_reason(client, db_session, image, capsys):
    newest = client.get(f"/golden-images/{image.id}/builds").json()["builds"][0]["build_id"]
    assert _script().promote(_Api(client), catalogue_ids=["srv2022"], reason="site acceptance 2026-11-23") == 0
    assert _promoted(client, image) == newest
    assert f"srv2022 -> {newest} promoted" in capsys.readouterr().out
    db_session.expire_all()
    (entry,) = db_session.query(AuditLog).filter_by(resource_id=str(image.id), action="golden_image.promote").all()
    assert json.loads(entry.detail)["reason"] == "site acceptance 2026-11-23"


def test_an_image_already_promoted_is_left_alone(client, db_session, image, capsys):
    mod = _script()
    assert client.post(f"/golden-images/{image.id}/builds/srv2022-b1/promote", json={"reason": "x"}).status_code == 200
    assert mod.promote(_Api(client), catalogue_ids=["srv2022"], reason="again") == 0
    assert _promoted(client, image) == "srv2022-b1"
    assert "already promoted (srv2022-b1); left alone" in capsys.readouterr().out
    db_session.expire_all()
    assert db_session.query(AuditLog).filter_by(resource_id=str(image.id), action="golden_image.promote").count() == 1


def test_a_missing_image_fails_and_the_others_are_still_promoted(client, image, capsys):
    assert _script().promote(_Api(client), catalogue_ids=["nope", "srv2022"], reason="r") == 1
    assert "FAIL nope: no such golden image" in capsys.readouterr().out
    assert _promoted(client, image) is not None


@pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.instructor])
def test_anyone_but_the_platform_admin_is_refused(client, db_session, tenant, image, role, capsys):
    act_as(real_user(db_session, role, tenant.id))
    assert _script().promote(_Api(client), catalogue_ids=["srv2022"], reason="r") == 1
    assert "FAIL" in capsys.readouterr().out
    act_as(real_user(db_session, UserRole.admin, tenant.id))
    assert _promoted(client, image) is None


def test_another_tenants_admin_is_refused(client, db_session, tenant, image):
    act_as(real_user(db_session, UserRole.admin, real_tenant(db_session, "promote-other").id))
    assert _script().promote(_Api(client), catalogue_ids=["srv2022"], reason="r") == 1
    act_as(real_user(db_session, UserRole.admin, tenant.id))
    assert _promoted(client, image) is None
