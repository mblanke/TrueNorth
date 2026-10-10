"""gs-core on vSphere (ADR 0007, slice 0): planning, the configure stage, breadcrumbs.

The VM build itself is vsphere_api's (tests/integration/test_vsphere_sim.py builds gs-core
against vcsim); here: what the worker adds to the rendered template, what the guest
receives (cloud-init + bundle), and how the configure stage and breadcrumb deliveries use
the backend's guest channel, with a fake provisioner standing in for VMware guest ops.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import uuid
from contextlib import contextmanager

import app.greyspace.models  # noqa: F401 — registers range_greyspace
import pytest
from app import models as m
from app.db import Base
from app.greyspace.models import RangeGreyspace
from app.greyspace.schemas import GreyspaceBlock
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker import base_tasks, configure_tasks, greyspace, greyspace_host, vyos_config  # noqa: E402
from worker.provisioners.base import GuestLogin  # noqa: E402

RANGE = "0f0f0f0f-1111-2222-3333-444444444444"


def _template(with_network: bool = True) -> dict:
    nets = [{"name": "corporate", "vlan_id": 110, "cidr": "10.40.110.0/24", "gateway": "10.40.110.1"}]
    if with_network:
        nets.append({"name": "greyspace", "vlan_id": 190, "cidr": "100.64.190.0/24", "gateway": "100.64.190.1"})
    edge = {
        "name": f"{RANGE[:8]}-edge01", "node_id": "edge01", "role": "router", "os": "vyos", "ip": "10.40.110.1",
        "nics": [
            {"vlan": 110, "network": "corporate", "ip": "10.40.110.1", "prefix": 24, "gateway": ""},
            {"vlan": 190, "network": "greyspace", "ip": "100.64.190.1", "prefix": 24, "gateway": ""},
        ],
    }
    ws = {"name": f"{RANGE[:8]}-ws01", "node_id": "ws01", "os": "windows-11", "ip": "10.40.110.10",
          "nics": [{"vlan": 110, "network": "corporate", "ip": "10.40.110.10", "prefix": 24, "gateway": "10.40.110.1"}]}
    return {"name": "r", "vms": [edge, ws], "networks": nets, "greyspace": {"site_packs": ["news", "webmail"]}}


def _bundle_files(vm: dict) -> dict[str, bytes]:
    gi = vm["guestinfo"]
    b64 = "".join(gi[f"guestinfo.tn.greyspace.bundle.{n}"] for n in range(int(gi["guestinfo.tn.greyspace.bundle.count"])))
    data = base64.b64decode(b64)
    import hashlib

    assert hashlib.sha256(data).hexdigest() == gi["guestinfo.tn.greyspace.bundle.sha256"]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {i.name: tar.extractfile(i).read() for i in tar.getmembers() if i.isfile()}


class TestHostVm:
    def test_gs_core_joins_the_greyspace_network_at_its_last_address(self):
        vm, info = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template())
        assert vm["name"] == f"{RANGE[:8]}-gs-core" and vm["node_id"] == "gs-core"
        assert vm["template_name"] == "greyspace-host"
        [nic] = vm["nics"]
        assert nic == {"vlan": 190, "network": "greyspace", "ip": "100.64.190.254", "prefix": 24,
                       "netmask": "255.255.255.0", "gateway": "100.64.190.1"}
        assert vm["dns"] == ["198.18.0.53"]
        assert info["public"] == "198.18.0.0/15" and info["router"] == "100.64.190.1"
        assert info["corpus"].startswith("bundled t0")

    def test_the_template_name_and_size_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GREYSPACE_HOST_TEMPLATE", "gs-host-v2")
        monkeypatch.setenv("GREYSPACE_HOST_MEMORY_MB", "16384")
        vm, _ = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template())
        assert vm["template_name"] == "gs-host-v2" and vm["memory_mb"] == 16384

    def test_no_greyspace_network_is_refused_with_the_fix(self):
        with pytest.raises(greyspace_host.HostPlanError, match="no network named 'greyspace'"):
            greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template(with_network=False))

    def test_the_bundle_carries_the_host_stack_and_the_t0_corpus(self):
        vm, _ = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template())
        files = _bundle_files(vm)
        compose = json.loads(files["stack/compose.yaml"])
        net = compose["networks"]["gs-public"]
        assert net["driver_opts"]["com.docker.network.bridge.gateway_mode_ipv4"] == "routed"
        assert "internal" not in net and "probe" not in compose["services"]
        assert "/srv/greyspace/corpus:/srv/corpus:ro" in compose["services"]["webfarm"]["volumes"]
        assert json.loads(files["stack/gs.json"])["host"] is True
        assert b"def plant(" in files["stack/bin/gs"]
        assert json.loads(files["corpus/manifest.json"])["tier"] == "t0"
        assert any(n.startswith("corpus/sites/maplewire-news.com/") for n in files)
        chunks = [k for k in vm["guestinfo"] if k.startswith("guestinfo.tn.greyspace.bundle.") and k[-1].isdigit()]
        assert all(len(vm["guestinfo"][k]) <= greyspace_host.CHUNK for k in chunks)

    def test_with_nfs_the_corpus_is_mounted_not_bundled(self, monkeypatch):
        monkeypatch.setenv("GREYSPACE_CORPUS_NFS", "netapp01:/greyspace-corpus")
        vm, info = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template())
        assert not any(n.startswith("corpus/") for n in _bundle_files(vm))
        env = next(f for f in vm["cloud_config"]["write_files"] if f["path"].endswith("host.env"))
        assert env["content"] == "GS_CORPUS_NFS=netapp01:/greyspace-corpus\n"
        assert info["corpus"] == "nfs netapp01:/greyspace-corpus"

    def test_cloud_init_creates_the_service_account_without_its_password(self):
        vm, _ = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), _template())
        cc = vm["cloud_config"]
        [user] = cc["users"]
        assert user["name"] == "tn-greyspace" and "docker" in user["groups"]
        password = greyspace_host.host_password(RANGE)
        if greyspace_host._sha512_crypt("probe") is not None:  # glibc (the worker image); not macOS
            assert password not in json.dumps(vm) and user["hashed_passwd"].startswith("$6$")
        assert cc["runcmd"] == [["/opt/greyspace/bootstrap.sh", "unpack"]]
        assert "vmware-rpctool" in next(f for f in cc["write_files"] if f["path"].endswith("bootstrap.sh"))["content"]

    def test_the_password_is_per_range_and_needs_the_secrets_key(self, monkeypatch):
        a, b = greyspace_host.host_password(RANGE), greyspace_host.host_password(str(uuid.uuid4()))
        assert a != b and a == greyspace_host.host_password(RANGE) and len(a) >= 32
        monkeypatch.setenv("TN_SECRETS_KEY", "")
        with pytest.raises(greyspace_host.HostPlanError, match="TN_SECRETS_KEY"):
            greyspace_host.host_password(RANGE)

    def test_the_router_on_the_network_gets_the_route_and_vyos_applies_it(self):
        template = _template()
        vm, info = greyspace_host.host_vm(RANGE, dict(greyspace.DEFAULT_BLOCK), template)
        assert greyspace_host.route_router(template, info) == f"{RANGE[:8]}-edge01"
        edge = template["vms"][0]
        assert edge["greyspace_route"] == {"prefix": "198.18.0.0/15", "via": "100.64.190.254",
                                           "resolver": "198.18.0.53", "network": "greyspace"}
        cfg = vyos_config.build_config(edge, networks=template["networks"])
        assert "set protocols static route '198.18.0.0/15' next-hop '100.64.190.254'" in cfg.commands
        assert "set firewall ipv4 forward filter rule 20 destination address '198.18.0.0/15'" in cfg.commands
        assert "set system name-server '198.18.0.53'" in cfg.commands

    def test_crumb_steps_carry_the_payload_for_bin_gs(self):
        payload = {"exercise": "ex-1", "crumbs": [{"id": "a", "kind": "dns", "name": "x.a.com", "value": "v"}]}
        [step] = greyspace_host.crumb_steps({"operation": "plant", "payload": payload})
        assert step.program == "/usr/bin/sudo" and "bin/gs crumb plant --b64 " in step.arguments
        assert json.loads(base64.b64decode(step.arguments.rsplit(" ", 1)[1])) == payload
        [rm] = greyspace_host.crumb_steps({"operation": "remove", "exercise": "ex-1", "ids": None})
        assert rm.arguments.endswith("crumb remove --exercise ex-1")


# ── the seam, with the API's schema in SQLite ─────────────────────────────────
@pytest.fixture
def factory(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    make = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    yield make
    eng.dispose()


@pytest.fixture
def rng(factory):
    db = factory()
    tenant = m.Tenant(name="gs", slug=f"gs-{uuid.uuid4().hex[:6]}")
    db.add(tenant)
    db.commit()
    tpl = m.Template(name="tpl", yaml="name: tpl\n", tenant_id=tenant.id)
    db.add(tpl)
    db.commit()
    r = m.Range(name="r", template_id=tpl.id, tenant_id=tenant.id, provisioner_backend="vsphere_api",
                state=m.RangeState.ready)
    db.add(r)
    db.commit()
    db.close()
    return r


def _row(factory, rng) -> RangeGreyspace | None:
    db = factory()
    try:
        return db.get(RangeGreyspace, rng.id)
    finally:
        db.close()


def _built(factory, rng, template):
    """What provision_range does on vSphere: plan gs-core, build (recorded), after_provision."""
    planned = greyspace.plan_host(str(rng.id), "vsphere_api", template)
    vms = [{"vm_id": f"vm-{n}", "name": v["name"], "node_id": v["node_id"]} for n, v in enumerate(planned["vms"])]
    db = factory()
    r = db.get(m.Range, rng.id)
    r.provisioner_output = json.dumps({"provider": "vsphere_api", "vms": vms})
    db.commit()
    db.close()
    return planned


class FakeVsphere:
    supports_guest_commands = True

    def __init__(self, statuses=None):
        self.calls: list[tuple] = []
        self.statuses = statuses or {}

    async def run_in_guest(self, vm_id, login, steps, timeout):
        self.calls.append((vm_id, login, [s.label for s in steps], [s.arguments for s in steps]))
        return [{"label": s.label, "status": self.statuses.get(s.label, "ok"),
                 "exit_code": 0 if self.statuses.get(s.label, "ok") == "ok" else 1} for s in steps]


def test_plan_host_adds_gs_core_and_after_provision_queues_configure(factory, rng, monkeypatch):
    sent = []
    monkeypatch.setattr(greyspace, "send_configure", sent.append)
    template = _template()
    planned = _built(factory, rng, template)
    assert [v["node_id"] for v in planned["vms"]] == ["edge01", "ws01", "gs-core"]
    assert "greyspace_route" not in template["vms"][0]  # the caller's template is not mutated
    row = _row(factory, rng)
    assert row.status == "configuring" and row.detail["stage"] == "build"
    assert row.detail["host"] == f"{str(rng.id)[:8]}-gs-core" and row.detail["router_vm"].endswith("edge01")
    GreyspaceBlock.model_validate(row.block)  # the template's block, attached by the worker
    assert greyspace.after_provision(str(rng.id), "vsphere_api", planned) == "configuring"
    assert sent == [str(rng.id)] and _row(factory, rng).detail["stage"] == "configure"


def test_plan_host_leaves_other_backends_and_plain_ranges_alone(factory, rng):
    template = _template()
    assert greyspace.plan_host(str(rng.id), "mock", template) is template
    plain = {k: v for k, v in template.items() if k != "greyspace"}
    assert greyspace.plan_host(str(rng.id), "vsphere_api", plain) is plain
    assert _row(factory, rng) is None


def test_a_template_without_the_network_builds_without_gs_core(factory, rng, monkeypatch):
    monkeypatch.setattr(greyspace, "send_configure", lambda rid: pytest.fail("must not configure"))
    template = _template(with_network=False)
    assert greyspace.plan_host(str(rng.id), "vsphere_api", template) is template
    row = _row(factory, rng)
    assert row.status == "failed" and row.detail["stage"] == "plan" and "greyspace" in row.detail["error"]
    assert greyspace.after_provision(str(rng.id), "vsphere_api", template) == "failed"


def test_configure_runs_the_steps_on_gs_core_and_records_deployed(factory, rng, monkeypatch):
    monkeypatch.setattr(greyspace, "send_configure", lambda rid: None)
    _built(factory, rng, _template())
    greyspace.after_provision(str(rng.id), "vsphere_api", {})
    fake = FakeVsphere()
    monkeypatch.setattr(base_tasks, "_get_backend", lambda backend, rid: fake)
    assert configure_tasks.configure_range.run(str(rng.id)) == {"range_id": str(rng.id), "greyspace": "deployed"}
    [(vm_id, login, labels, args)] = fake.calls
    assert vm_id == "vm-2" and labels == ["cloud-init", "configure", "health"]
    assert login == GuestLogin("tn-greyspace", greyspace_host.host_password(str(rng.id)), "linux")
    assert args[1].endswith("/opt/greyspace/bootstrap.sh configure")
    row = _row(factory, rng)
    assert row.status == "deployed" and row.deployed_at is not None
    assert [s["status"] for s in row.detail["configure"]] == ["ok", "ok", "ok"]
    assert "note" not in row.detail and row.detail["ip"].endswith(".254")


def test_a_failed_step_is_recorded_with_its_name(factory, rng, monkeypatch):
    monkeypatch.setattr(greyspace, "send_configure", lambda rid: None)
    _built(factory, rng, _template())
    monkeypatch.setattr(base_tasks, "_get_backend", lambda backend, rid: FakeVsphere({"health": "failed"}))
    assert greyspace.configure(str(rng.id)) == "failed"
    row = _row(factory, rng)
    assert row.detail["error"] == "step health failed (exit 1)" and row.deployed_at is None


def test_configure_without_gs_core_in_the_build_fails_cleanly(factory, rng, monkeypatch):
    db = factory()
    db.add(RangeGreyspace(range_id=rng.id, tenant_id=rng.tenant_id, block=GreyspaceBlock().model_dump(),
                          corpus_tier="t0", status="configuring"))
    db.commit()
    db.close()
    assert greyspace.configure(str(rng.id)) == "failed"
    assert "gs-core is not among" in _row(factory, rng).detail["error"]


def test_breadcrumbs_on_mock_are_recorded(factory, rng):
    db = factory()
    db.add(RangeGreyspace(range_id=rng.id, tenant_id=rng.tenant_id, block=GreyspaceBlock().model_dump(),
                          corpus_tier="t0", status="deployed"))
    db.commit()
    db.close()
    op = {"operation": "plant", "exercise": "ex-1", "payload": {"exercise": "ex-1", "crumbs": [{"id": "a"}]}}
    ok, what = greyspace.deliver_breadcrumbs(str(rng.id), "mock", op)
    assert ok and "recorded" in what
    [entry] = _row(factory, rng).detail["breadcrumbs"]
    assert entry["exercise"] == "ex-1" and entry["ids"] == ["a"] and entry["ok"] is True


def test_breadcrumbs_on_vsphere_go_to_gs_core_once_deployed(factory, rng, monkeypatch):
    monkeypatch.setattr(greyspace, "send_configure", lambda rid: None)
    _built(factory, rng, _template())
    fake = FakeVsphere()
    monkeypatch.setattr(base_tasks, "_get_backend", lambda backend, rid: fake)
    op = {"operation": "plant", "exercise": "ex-1", "payload": {"exercise": "ex-1", "crumbs": [{"id": "a"}]}}
    ok, what = greyspace.deliver_breadcrumbs(str(rng.id), "vsphere_api", op)
    assert not ok and "not deployed" in what  # still configuring: nothing sent
    assert fake.calls == []
    greyspace.configure(str(rng.id))
    ok, what = greyspace.deliver_breadcrumbs(str(rng.id), "vsphere_api", op)
    assert ok and what == "plant on gs-core"
    assert fake.calls[-1][2] == ["crumb-plant"]


def test_breadcrumbs_need_an_attached_block(factory, rng):
    ok, what = greyspace.deliver_breadcrumbs(str(rng.id), "mock", {"operation": "remove", "exercise": "e"})
    assert not ok and "no Greyspace block" in what
