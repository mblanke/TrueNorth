"""Worker tasks around vSphere ranges: VLAN reservation, provision wiring, stop/start.

Run against a real (SQLite) database through the tasks' own raw SQL, so the
statements the worker sends are the ones tested.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from worker import tasks, vlan_pool
from worker.provisioners.results import ProvisionResult, StartResult, StopResult

SCHEMA = [
    "CREATE TABLE ranges (id TEXT PRIMARY KEY, state TEXT, provisioner_output TEXT, provisioner_backend TEXT,"
    " template_id TEXT, error_message TEXT, updated_at TEXT)",
    "CREATE TABLE templates (id TEXT PRIMARY KEY, yaml TEXT)",
    "CREATE TABLE golden_images (catalogue_id TEXT, template_name TEXT, os_aliases TEXT, hypervisor TEXT,"
    " enabled BOOLEAN, deleted_at TEXT)",
    "CREATE TABLE hypervisor_connections (host TEXT, port INT, username TEXT, password_encrypted TEXT,"
    " api_token TEXT, verify_ssl BOOLEAN, datacenter TEXT, hypervisor_type TEXT, is_active BOOLEAN,"
    " is_primary BOOLEAN)",
]


def _sqlite_now(dbapi_conn, _record):
    """The worker's SQL is written for Postgres: give SQLite its NOW()."""
    if hasattr(dbapi_conn, "create_function"):
        dbapi_conn.create_function("NOW", 0, lambda: "2026-10-03 00:00:00")


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for stmt in SCHEMA:
            conn.execute(sa.text(stmt))
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    monkeypatch.setattr(tasks, "_vlan_lock", contextlib.nullcontext)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setenv("VSPHERE_VLAN_POOL", "100-199")

    def run(sql, **params):
        with engine.begin() as conn:
            return conn.execute(sa.text(sql), params).fetchall() if sql.lstrip().upper().startswith("SELECT") \
                else conn.execute(sa.text(sql), params)

    yield run
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)
    engine.dispose()


def _range(db, rid, state, physical=None, backend="vsphere_api", template_id=None):
    out = None
    if physical is not None:
        out = json.dumps({"vms": [], "networks": [{"vlan_id": k, "physical_vlan": v} for k, v in physical.items()]})
    db("INSERT INTO ranges (id, state, provisioner_output, provisioner_backend, template_id) "
       "VALUES (:id, :s, :o, :b, :t)", id=rid, s=state, o=out, b=backend, t=template_id)


def _output(db, rid) -> dict:
    return json.loads(db("SELECT provisioner_output FROM ranges WHERE id = :id", id=rid)[0][0])


class TestReserveVlans:
    def test_skips_vlans_other_live_ranges_hold_and_records_the_reservation(self, db):
        _range(db, "other", "ready", {200: 100, 201: 101})
        _range(db, "failed-one", "failed", {200: 103})  # may still have its port groups
        _range(db, "gone", "destroyed", {200: 102})  # destroyed: its VLAN is free again
        _range(db, "mine", "provisioning")

        got = tasks._reserve_vlans("mine", [200, 201])

        assert got == {200: 102, 201: 104}
        nets = _output(db, "mine")["networks"]
        assert nets == [
            {"vlan_id": 200, "physical_vlan": 102, "portgroup": "tn-mine-v102", "switch_mode": "vds"},
            {"vlan_id": 201, "physical_vlan": 104, "portgroup": "tn-mine-v104", "switch_mode": "vds"},
        ]

    def test_a_retry_keeps_its_reservation(self, db):
        _range(db, "mine", "provisioning")
        first = tasks._reserve_vlans("mine", [200, 201])
        _range(db, "later", "provisioning")
        assert tasks._reserve_vlans("later", [200]) == {200: 102}
        assert tasks._reserve_vlans("mine", [200, 201]) == first

    def test_exhausted_pool_raises(self, db, monkeypatch):
        monkeypatch.setenv("VSPHERE_VLAN_POOL", "100-101")
        _range(db, "other", "ready", {200: 100})
        _range(db, "mine", "provisioning")
        with pytest.raises(vlan_pool.VlanPoolExhaustedError):
            tasks._reserve_vlans("mine", [200, 201])


class FakeProvisioner:
    def __init__(self):
        self.calls: list = []

    async def provision(self, range_id, template, allocations):
        self.calls.append(("provision", template, allocations))
        nets = [{**n, "physical_vlan": allocations["physical_vlans"][n["vlan_id"]]} for n in template["networks"]]
        return ProvisionResult(status="ok", vms=[{"name": v["name"], "vm_id": "vm-1"} for v in template["vms"]],
                               networks=nets)

    async def stop(self, range_id, output):
        self.calls.append(("stop", output))
        return StopResult(status="ok", vms_stopped=1)

    async def start(self, range_id, output):
        self.calls.append(("start", output))
        return StartResult(status="failed", errors=["vm-1: host down"])


def test_provision_range_reserves_vlans_for_vsphere(db, monkeypatch):
    fake = FakeProvisioner()
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: fake)
    _range(db, "other", "ready", {200: 100})
    db("INSERT INTO templates (id, yaml) VALUES ('t1', :y)", y=json.dumps({
        "name": "lab",
        "network": {"vlans": [{"id": 200, "name": "lan", "cidr": "10.1.0.0/24"},
                              {"id": 201, "name": "dmz", "cidr": "10.2.0.0/24"}]},
        "nodes": [{"id": "fw", "os": "pfsense", "role": "firewall", "vlan": "lan",
                   "interfaces": [{"vlan": "lan"}, {"vlan": "dmz"}]}],
    }))
    _range(db, "r1234567-x", "provisioning", template_id="t1")

    tasks.provision_range.run("r1234567-x")

    (_, template, allocations) = fake.calls[0]
    assert allocations["physical_vlans"] == {200: 101, 201: 102}
    assert [n["vlan"] for n in template["vms"][0]["nics"]] == [200, 201]
    row = db("SELECT state, provisioner_output FROM ranges WHERE id = 'r1234567-x'")[0]
    assert row[0] == "ready"
    assert {n["vlan_id"]: n["physical_vlan"] for n in json.loads(row[1])["networks"]} == {200: 101, 201: 102}


class TestReserveUplinkIp:
    @pytest.fixture(autouse=True)
    def _pool(self, monkeypatch):
        monkeypatch.setenv("VSPHERE_RANGE_UPLINK_POOL", "10.30.32.100-10.30.32.102")
        monkeypatch.setenv("VSPHERE_RANGE_UPLINK_NETWORK", "dPG-TN-SVC")

    def _with_uplink(self, db, rid, state, ip):
        db("INSERT INTO ranges (id, state, provisioner_output, provisioner_backend) VALUES (:id, :s, :o, 'vsphere_api')",
           id=rid, s=state, o=json.dumps({"vms": [], "networks": [], "uplink": {"ip": ip}}))

    def test_skips_addresses_live_ranges_hold_and_records_it(self, db):
        self._with_uplink(db, "other", "ready", "10.30.32.100")
        self._with_uplink(db, "gone", "destroyed", "10.30.32.101")  # destroyed: free again
        _range(db, "mine", "provisioning", {200: 150})
        assert tasks._reserve_uplink_ip("mine") == "10.30.32.101"
        out = _output(db, "mine")
        assert out["uplink"] == {"network": "dPG-TN-SVC", "ip": "10.30.32.101"}
        assert out["networks"] == [{"vlan_id": 200, "physical_vlan": 150}]  # the VLAN reservation stays
        assert tasks._reserve_uplink_ip("mine") == "10.30.32.101"  # a retry keeps it

    def test_exhausted(self, db):
        for i, ip in enumerate(("10.30.32.100", "10.30.32.101", "10.30.32.102")):
            self._with_uplink(db, f"r{i}", "failed", ip)  # a failed range may still be wired
        _range(db, "mine", "provisioning")
        with pytest.raises(RuntimeError, match="uplink pool"):
            tasks._reserve_uplink_ip("mine")


def test_provision_range_reserves_the_uplink_and_stores_it(db, monkeypatch):
    monkeypatch.setenv("VSPHERE_RANGE_UPLINK_POOL", "10.30.32.100-10.30.32.102")
    monkeypatch.setenv("VSPHERE_RANGE_UPLINK_NETWORK", "dPG-TN-SVC")
    fake = FakeProvisioner()

    async def provision(range_id, template, allocations):
        fake.calls.append(("provision", template, allocations))
        return ProvisionResult(status="ok", vms=[{"name": "fw", "vm_id": "vm-1"}], networks=[],
                               warnings=["software 'x' skipped"],
                               uplink={"network": "dPG-TN-SVC", "ip": allocations["uplink_ip"]})

    fake.provision = provision
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: fake)
    db("INSERT INTO templates (id, yaml) VALUES ('t1', :y)", y=json.dumps({
        "name": "lab", "network": {"vlans": [{"id": 200, "name": "lan", "cidr": "10.1.0.0/24"}]},
        "nodes": [{"id": "fw", "os": "pfsense", "role": "firewall", "vlan": "lan"}],
    }))
    _range(db, "r1234567-x", "provisioning", template_id="t1")

    tasks.provision_range.run("r1234567-x")

    assert fake.calls[0][2]["uplink_ip"] == "10.30.32.100"
    out = _output(db, "r1234567-x")
    assert out["uplink"]["ip"] == "10.30.32.100" and out["warnings"] == ["software 'x' skipped"]


def _task(last: bool = True):
    return SimpleNamespace(request=SimpleNamespace(called_directly=last, retries=0), max_retries=3)


def test_stop_range_powers_off_through_the_ranges_backend(db, monkeypatch):
    fake = FakeProvisioner()
    seen = []
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: seen.append(backend) or fake)
    _range(db, "r1", "stopped", {200: 100})

    assert tasks._power_range(_task(), "r1", "stop", "stopped") == {"status": "stopped", "range_id": "r1"}
    assert seen == ["vsphere_api"] and fake.calls[0][0] == "stop"
    assert db("SELECT state FROM ranges WHERE id = 'r1'")[0][0] == "stopped"


def test_failed_start_marks_the_range_failed_on_the_last_attempt(db, monkeypatch):
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: FakeProvisioner())
    _range(db, "r1", "running", {200: 100})
    with pytest.raises(RuntimeError, match="host down"):
        tasks._power_range(_task(last=False), "r1", "start", "running")
    assert db("SELECT state FROM ranges WHERE id = 'r1'")[0][0] == "running"  # a retry is coming
    with pytest.raises(RuntimeError):
        tasks._power_range(_task(), "r1", "start", "running")
    state, error = db("SELECT state, error_message FROM ranges WHERE id = 'r1'")[0]
    assert state == "failed" and "host down" in error


def test_tasks_are_registered_and_routed():
    from worker.celery_app import app

    assert {"worker.tasks.stop_range", "worker.tasks.start_range"} <= set(app.tasks)
    assert app.conf.task_routes["worker.tasks.stop_range"] == {"queue": "provision"}


def test_vsphere_backend_takes_db_credentials_without_vsphere_url(db, monkeypatch):
    monkeypatch.delenv("VSPHERE_URL", raising=False)
    db("INSERT INTO hypervisor_connections VALUES ('vcsa.truenorth.lab', 443, 'svc@vsphere.local', 'pw', '',"
       " 0, 'DC-Lab', 'vsphere', 1, 1)")
    prov = tasks._get_backend("vsphere_api")
    assert prov._base_url == "https://vcsa.truenorth.lab" and prov._username == "svc@vsphere.local"
