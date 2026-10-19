"""scripts/register_golden_image_inprocess.py against the real /golden-images router.

The contract tests (tests/contracts/test_work_site_template_kit.py) use a fake API; this
runs ``register()`` through the API's own routes, schemas and the infra:write guard: a
catalogue image is marked built with its template, a re-run changes nothing, a missing
variant is created, and a role without infra:write is refused (exit 1, nothing written).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import GoldenImage, UserRole

ROOT = Path(__file__).resolve().parents[2]


def _script():
    path = ROOT / "scripts/register_golden_image_inprocess.py"
    spec = importlib.util.spec_from_file_location("register_golden_image_inprocess", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Api:
    def __init__(self, client):
        self.client = client

    def _call(self, method, path, json=None, params=None):
        r = self.client.request(method, path, json=json, params=params)
        return r.status_code, (r.json() if r.content else None)

    def get(self, path, params=None, **kw):
        return self._call("GET", path, params=params)

    def post(self, path, json=None, **kw):
        return self._call("POST", path, json)

    def patch(self, path, json=None, **kw):
        return self._call("PATCH", path, json)


@pytest.fixture
def seeded(db_session):
    tenant = real_tenant(db_session, "work-templates")
    db_session.add(
        GoldenImage(
            catalogue_id="srv2022", hypervisor="vsphere", template_name="", enabled=True, build_status="planned"
        )
    )
    db_session.add(
        GoldenImage(
            catalogue_id="srv2022",
            hypervisor="proxmox",
            template_name="tmpl-srv2022",
            enabled=True,
            build_status="built",
        )
    )
    db_session.commit()
    return tenant


def _row(db_session, cid, hv="vsphere"):
    db_session.expire_all()
    return db_session.query(GoldenImage).filter_by(catalogue_id=cid, hypervisor=hv).one_or_none()


def test_marks_a_catalogue_image_built_once(client, db_session, seeded, capsys):
    act_as(real_user(db_session, UserRole.admin, seeded.id))
    mod, api = _script(), _Api(client)
    assert mod.register(api, catalogue_id="srv2022", template="srv2022") == 0
    row = _row(db_session, "srv2022")
    assert (row.build_status, row.template_name) == ("built", "srv2022")
    assert _row(db_session, "srv2022", "proxmox").template_name == "tmpl-srv2022"  # other hypervisor untouched
    assert "srv2022 -> built" in capsys.readouterr().out

    assert mod.register(api, catalogue_id="srv2022", template="srv2022") == 0
    assert "unchanged" in capsys.readouterr().out


def test_creates_a_missing_variant(client, db_session, seeded):
    act_as(real_user(db_session, UserRole.admin, seeded.id))
    variant = {"os_family": "linux", "version": "24.04", "role": "web server", "os_aliases": ["ubuntu-24.04-webstack"]}
    assert (
        _script().register(_Api(client), catalogue_id="ubuntu-webstack", template="ubuntu-webstack", variant=variant)
        == 0
    )
    row = _row(db_session, "ubuntu-webstack")
    assert row is not None and (row.build_status, row.template_name) == ("built", "ubuntu-webstack")


def test_missing_catalogue_image_is_an_error(client, db_session, seeded, capsys):
    act_as(real_user(db_session, UserRole.admin, seeded.id))
    assert _script().register(_Api(client), catalogue_id="kali", template="kali") == 1
    assert _row(db_session, "kali") is None


def test_a_role_without_infra_write_is_refused(client, db_session, seeded):
    act_as(real_user(db_session, UserRole.instructor, seeded.id))
    assert _script().register(_Api(client), catalogue_id="srv2022", template="srv2022") == 1
    assert _row(db_session, "srv2022").build_status == "planned"
