"""Pinning every range VM to its golden image's promoted build (ADR 0013 §2).

Builds are registered and promoted through the API's own service (app/golden_image_builds)
on in-memory SQLite created from the API's models, and the worker's pin step
(worker/template_pins.py) reads them back through its own SQL (db_ops). The provision task
is then run with a fake backend: a VM that cannot be pinned fails the range before any
backend call, with no retry; a pinned one reaches the backend carrying its build.

The vSphere side (placement on hosts holding the copy, stamp checks, no fallback) is in
test_vsphere_provision.py ``TestTemplatePinning``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import app.golden_image_builds  # noqa: F401 — registers golden_image_builds
import pytest
from app import models as m
from app.auth import CurrentUser
from app.db import Base
from app.golden_image_builds import service
from app.golden_image_builds.schemas import GoldenImageBuildIn
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker import template_pins  # noqa: E402
from worker.fencing import FINAL_ERRORS  # noqa: E402
from worker.provisioners.results import ProvisionResult  # noqa: E402

SUM = "a" * 64
ADMIN = CurrentUser(id=str(uuid.uuid4()), email="admin@example.org", display_name="Admin", role=m.UserRole.admin,
                    tenant_id="", keycloak_id="kc-admin")


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)

    @contextmanager
    def session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", session)
    monkeypatch.setattr(tasks, "_notify_api", MagicMock())
    monkeypatch.delenv(template_pins.FLAG, raising=False)
    reader = factory()
    reader.session_factory = session
    yield reader
    reader.close()
    eng.dispose()


def _image(db, cid: str, template: str = "", status: str = "built", aliases: str = "[]") -> m.GoldenImage:
    img = m.GoldenImage(catalogue_id=cid, os_family="linux", hypervisor="vsphere", template_name=template or cid,
                        os_aliases=aliases, build_status=status, enabled=True)
    db.add(img)
    db.commit()
    return img


def _register(db, img, build_id: str, hosts=("esx02", "esx03"), checksum: str = SUM):
    body = GoldenImageBuildIn(build_id=build_id, checksum=checksum, build_date=date(2026, 10, 19),
                              iso_checksum="sha256:" + "c" * 64,
                              copies={h: f"{img.template_name}@{h}-20261019" for h in hosts})
    service.record_build(db, img, body, ADMIN)
    db.commit()


def _pin(db, vms, backend="vsphere_api"):
    return template_pins.pin(db.session_factory, backend, {"vms": vms})


def _vm(name, template):
    return {"name": name, "template_name": template}


class TestPin:
    def test_every_vm_of_one_image_gets_the_one_promoted_build(self, db):
        img = _image(db, "ubuntu-2404", "tmpl-ubuntu-2404")
        _register(db, img, "b1")
        _register(db, img, "b2", checksum="b" * 64)  # newer, not promoted
        service.promote(db, img, "b1", ADMIN)
        out = _pin(db, [_vm("web01", "tmpl-ubuntu-2404"), _vm("web02", "tmpl-ubuntu-2404")])
        builds = [v["template_build"] for v in out["vms"]]
        assert builds[0] == builds[1] == {
            "promoted": True, "catalogue_id": "ubuntu-2404", "build_id": "b1", "checksum": SUM,
            "build_date": "2026-10-19", "iso_checksum": "sha256:" + "c" * 64, "template_name": "tmpl-ubuntu-2404",
            "copies": {"esx02": "tmpl-ubuntu-2404@esx02-20261019", "esx03": "tmpl-ubuntu-2404@esx03-20261019"}}

    def test_an_unpromoted_image_fails_with_every_vm_named(self, db):
        _image(db, "ubuntu-2404", "tmpl-ubuntu-2404")
        _image(db, "kali")
        with pytest.raises(template_pins.TemplatePinError) as err:
            _pin(db, [_vm("web01", "tmpl-ubuntu-2404"), _vm("att01", "kali")])
        assert "VM 'web01': golden image 'ubuntu-2404' has no promoted build" in str(err.value)
        assert "VM 'att01': golden image 'kali' has no promoted build" in str(err.value)
        assert template_pins.TemplatePinError in FINAL_ERRORS  # no retry: nothing would change

    def test_a_template_that_is_not_a_golden_image_fails(self, db):
        with pytest.raises(template_pins.TemplatePinError, match="'ubuntu-2404-cloud' is not an enabled golden image"):
            _pin(db, [_vm("web01", "ubuntu-2404-cloud")])

    def test_a_disabled_image_is_not_pinned(self, db):
        img = _image(db, "kali")
        _register(db, img, "b1")
        service.promote(db, img, "b1", ADMIN)
        img.enabled = False
        db.commit()
        with pytest.raises(template_pins.TemplatePinError, match="not an enabled golden image"):
            _pin(db, [_vm("att01", "kali")])

    def test_other_backends_are_untouched(self, db):
        vms = [_vm("web01", "anything")]
        assert _pin(db, vms, backend="mock") == {"vms": vms}
        assert _pin(db, vms, backend="proxmox") == {"vms": vms}

    def test_a_pinned_role_image_has_no_fallback(self, db):
        role = _image(db, "srv2022-exchange2019")
        _image(db, "srv2022")
        _register(db, role, "r1")
        service.promote(db, role, "r1", ADMIN)
        out = _pin(db, [{**_vm("exch", "srv2022-exchange2019"), "role_fallback_template": "srv2022"}])
        vm = out["vms"][0]
        assert vm["template_name"] == "srv2022-exchange2019" and "role_fallback_template" not in vm
        assert vm["template_build"]["build_id"] == "r1"

    def test_an_unpromoted_role_image_fails_clearly_never_the_plain_os(self, db):
        """Its OS image is promoted, the role image is not: the VM fails, it does not quietly
        become a bare Server 2022."""
        _image(db, "srv2022-exchange2019")
        plain = _image(db, "srv2022")
        _register(db, plain, "s1")
        service.promote(db, plain, "s1", ADMIN)
        with pytest.raises(template_pins.TemplatePinError) as err:
            _pin(db, [{**_vm("exch", "srv2022-exchange2019"), "role_fallback_template": "srv2022"}])
        assert "golden image 'srv2022-exchange2019' has no promoted build" in str(err.value)
        assert "the plain OS 'srv2022' is not built in its place" in str(err.value)


class TestTransitionFlag:
    def test_off_by_default(self, db):
        _image(db, "ubuntu-2404")
        with pytest.raises(template_pins.TemplatePinError):
            _pin(db, [_vm("web01", "ubuntu-2404")])

    def test_true_allows_a_built_unpromoted_image(self, db, monkeypatch, caplog):
        monkeypatch.setenv(template_pins.FLAG, "true")
        _image(db, "ubuntu-2404")
        caplog.set_level("WARNING")
        vm = _pin(db, [_vm("web01", "ubuntu-2404")])["vms"][0]
        assert vm["template_build"] == {"promoted": False, "catalogue_id": "ubuntu-2404",
                                        "template_name": "ubuntu-2404", "allowed_by": template_pins.FLAG}
        assert "UNPROMOTED golden images in this range" in caplog.text

    def test_a_named_list_allows_only_the_named_image(self, db, monkeypatch):
        monkeypatch.setenv(template_pins.FLAG, "ubuntu-2404")
        _image(db, "ubuntu-2404")
        _image(db, "kali")
        assert _pin(db, [_vm("web01", "ubuntu-2404")])["vms"][0]["template_build"]["promoted"] is False
        with pytest.raises(template_pins.TemplatePinError, match="does not name it"):
            _pin(db, [_vm("att01", "kali")])

    def test_an_image_that_is_not_built_is_never_allowed(self, db, monkeypatch):
        monkeypatch.setenv(template_pins.FLAG, "true")
        _image(db, "ubuntu-2404", status="planned")
        with pytest.raises(template_pins.TemplatePinError, match="allows only built images"):
            _pin(db, [_vm("web01", "ubuntu-2404")])

    def test_it_switches_off_per_image_once_that_image_is_promoted(self, db, monkeypatch):
        monkeypatch.setenv(template_pins.FLAG, "true")
        ubuntu = _image(db, "ubuntu-lts")
        kali = _image(db, "kali")
        _register(db, kali, "k1")
        service.promote(db, kali, "k1", ADMIN)  # another image's promotion changes nothing for ubuntu
        assert _pin(db, [_vm("web01", "ubuntu-lts")])["vms"][0]["template_build"]["promoted"] is False
        _register(db, ubuntu, "u1")
        service.promote(db, ubuntu, "u1", ADMIN)  # now ubuntu always clones its build, flag or not
        assert _pin(db, [_vm("web01", "ubuntu-lts")])["vms"][0]["template_build"]["build_id"] == "u1"
        assert _pin(db, [_vm("att01", "kali")])["vms"][0]["template_build"]["build_id"] == "k1"


class TestGreyspaceHost:
    """gs-core (greyspace_host.host_template(): ``greyspace-host``) is a VM the worker adds
    itself; it pins like any image, and is the only unregistered template the flag may pass."""

    def test_the_allow_list_is_exactly_greyspace_host(self):
        from worker import greyspace_host

        assert template_pins.PLATFORM_TEMPLATES == {"greyspace-host": "greyspace-host"}
        assert greyspace_host.host_template() in template_pins.PLATFORM_TEMPLATES

    def test_the_catalogue_carries_greyspace_host(self):
        from pathlib import Path

        from app.golden_images import parse_catalogue

        csv = (Path(__file__).resolve().parents[2] / "content/catalogue/vm_iso_catalogue.csv").read_text()
        (row,) = [r for r in parse_catalogue(csv) if r["catalogue_id"] == "greyspace-host"]
        assert row["template_name"] == "greyspace-host"

    def test_registered_and_promoted_it_pins_to_its_build(self, db):
        img = _image(db, "greyspace-host")
        _register(db, img, "g1")
        service.promote(db, img, "g1", ADMIN)
        vm = _pin(db, [_vm("web", "greyspace-host")])["vms"][0]
        assert vm["template_build"]["build_id"] == "g1"

    def test_unregistered_it_fails_without_the_flag(self, db):
        with pytest.raises(template_pins.TemplatePinError, match="import the catalogue's 'greyspace-host' row"):
            _pin(db, [_vm("r-gs-core", "greyspace-host")])

    @pytest.mark.parametrize("flag", ["true", "greyspace-host", "ubuntu-lts,greyspace-host"])
    def test_unregistered_the_flag_lets_it_through_by_name(self, db, monkeypatch, flag):
        monkeypatch.setenv(template_pins.FLAG, flag)
        vm = _pin(db, [_vm("r-gs-core", "greyspace-host")])["vms"][0]
        assert vm["template_build"] == {"promoted": False, "catalogue_id": "greyspace-host",
                                        "template_name": "greyspace-host", "allowed_by": template_pins.FLAG}

    def test_the_flag_never_passes_another_unregistered_template(self, db, monkeypatch):
        monkeypatch.setenv(template_pins.FLAG, "true")
        with pytest.raises(template_pins.TemplatePinError, match="'tmpl-other' is not an enabled golden image"):
            _pin(db, [_vm("x", "tmpl-other")])

    def test_a_named_list_without_it_refuses_it(self, db, monkeypatch):
        monkeypatch.setenv(template_pins.FLAG, "ubuntu-lts")
        with pytest.raises(template_pins.TemplatePinError):
            _pin(db, [_vm("r-gs-core", "greyspace-host")])


class TestProvisionTask:
    """The pin step in worker.tasks.provision_range, end to end with a fake backend."""

    @pytest.fixture
    def rng(self, db):
        tenant = m.Tenant(name="t1", slug="t1")
        db.add(tenant)
        db.commit()
        tmpl = m.Template(name="tpl", yaml="nodes:\n  - id: web01\n    os: ubuntu-2404\n", tenant_id=tenant.id)
        db.add(tmpl)
        db.commit()
        r = m.Range(name="r1", template_id=tmpl.id, tenant_id=tenant.id, provisioner_backend="vsphere_api",
                    state=m.RangeState.provisioning)
        db.add(r)
        db.commit()
        return r

    def _backend(self, monkeypatch):
        backend = MagicMock()
        backend.provision = AsyncMock(return_value=ProvisionResult(status="ok", vms=[{"name": "web01"}],
                                                                   networks=[]))
        backend.allocation_needs = MagicMock(return_value=[])
        monkeypatch.setattr(tasks, "_get_backend", lambda name=None, range_id=None, **kw: backend)
        return backend

    def test_an_unpromoted_image_fails_the_range_before_any_backend_call(self, db, rng, monkeypatch):
        _image(db, "ubuntu-2404", "tmpl-ubuntu-2404", aliases='["ubuntu-2404"]')
        backend = self._backend(monkeypatch)
        with pytest.raises(template_pins.TemplatePinError):
            tasks.provision_range(str(rng.id))
        db.expire_all()
        got = db.get(m.Range, rng.id)
        assert got.state == m.RangeState.failed and "has no promoted build" in got.error_message
        backend.provision.assert_not_awaited()

    def test_a_promoted_build_reaches_the_backend_on_every_vm(self, db, rng, monkeypatch):
        img = _image(db, "ubuntu-2404", "tmpl-ubuntu-2404", aliases='["ubuntu-2404"]')
        _register(db, img, "b1")
        service.promote(db, img, "b1", ADMIN)
        backend = self._backend(monkeypatch)
        tasks.provision_range(str(rng.id))
        vms = backend.provision.await_args.args[1]["vms"]
        assert vms and all(v["template_build"]["build_id"] == "b1" for v in vms)

    @staticmethod
    def _with_gs_core(monkeypatch):
        """greyspace.plan_host as it is on a range with a Greyspace block: one gs-core VM added."""
        from worker import greyspace, greyspace_host

        def plan_host(range_id, backend, template):
            gs = {"name": f"{range_id[:8]}-gs-core", "template_name": greyspace_host.host_template()}
            return {**template, "vms": [*template["vms"], gs]}

        monkeypatch.setattr(greyspace, "plan_host", plan_host)

    def test_greyspace_gs_core_is_pinned_to_its_promoted_build(self, db, rng, monkeypatch):
        img = _image(db, "ubuntu-2404", "tmpl-ubuntu-2404", aliases='["ubuntu-2404"]')
        gs = _image(db, "greyspace-host")
        for i, b in ((img, "b1"), (gs, "g1")):
            _register(db, i, b)
            service.promote(db, i, b, ADMIN)
        self._with_gs_core(monkeypatch)
        backend = self._backend(monkeypatch)
        tasks.provision_range(str(rng.id))
        vms = {v["template_name"]: v for v in backend.provision.await_args.args[1]["vms"]}
        assert vms["greyspace-host"]["template_build"]["build_id"] == "g1"
        assert vms["tmpl-ubuntu-2404"]["template_build"]["build_id"] == "b1"

    def _ubuntu_promoted_with_gs_core(self, db, monkeypatch):
        img = _image(db, "ubuntu-2404", "tmpl-ubuntu-2404", aliases='["ubuntu-2404"]')
        _register(db, img, "b1")
        service.promote(db, img, "b1", ADMIN)
        self._with_gs_core(monkeypatch)
        return self._backend(monkeypatch)

    def test_greyspace_gs_core_unregistered_fails_the_build_without_the_flag(self, db, rng, monkeypatch):
        backend = self._ubuntu_promoted_with_gs_core(db, monkeypatch)
        with pytest.raises(template_pins.TemplatePinError, match="greyspace-host"):
            tasks.provision_range(str(rng.id))
        backend.provision.assert_not_awaited()

    def test_greyspace_gs_core_unregistered_passes_with_the_flag_naming_it(self, db, rng, monkeypatch):
        backend = self._ubuntu_promoted_with_gs_core(db, monkeypatch)
        monkeypatch.setenv(template_pins.FLAG, "greyspace-host")
        tasks.provision_range(str(rng.id))
        vms = {v["template_name"]: v for v in backend.provision.await_args.args[1]["vms"]}
        assert vms["greyspace-host"]["template_build"]["promoted"] is False
